#!/usr/bin/env python3
"""Exporta os cartões aprovados de cards/<projeto>.json para um .apkg real.

Uso:
  python scripts/export_apkg.py cards/<projeto>.json --check    confere, sem exportar
  python scripts/export_apkg.py cards/<projeto>.json [--out exports/<projeto>.apkg] [--modern]

Usa a biblioteca oficial `anki` (o mesmo backend do Anki Desktop): modelos próprios para
Basic e Cloze e o modelo nativo de Image Occlusion (Anki 23.10+), com máscaras editáveis.

Erros (qualquer um, em cartão aprovado, cancela a exportação inteira):
  - metadados de rastreabilidade incompletos (seção 08 do CLAUDE.md); fonte não autorizada;
  - trecho de evidência que não existe literalmente na página citada;
  - número visível no cartão que não está nos trechos de evidência;
  - termo da resposta (verso, extra, frase do cloze) que não existe nas fontes autorizadas;
  - estrutura inválida: campo vazio, lacuna, máscara, mídia, card_id ou frente repetidos.
Avisos (relatados ao usuário, não bloqueiam): termo fora da página citada, termo da pergunta
ou da vinheta ausente das fontes, sigla que a fonte não usa, verso com mais de 25 palavras,
lista sem a quantidade na frente, mais de 3 lacunas, evidência de transcrição visual.
Cartões com validation_status diferente de "approved" não são exportados.

As travas são lexicais. Provam que o cartão não cita o que a fonte não diz; não provam que a
frase foi bem interpretada. Essa parte é a auditoria semântica, feita na elaboração.

Identificadores estáveis: o GUID da nota deriva de projeto + card_id, e os IDs dos modelos,
campos e templates são fixos. Reimportar um pacote atualizado atualiza as notas existentes
em vez de duplicá-las. Não reutilize um card_id para um cartão diferente.

Datas de modificação: o Anki só sobrescreve uma nota quando a versão importada é mais
recente. Por isso cada nota guarda a data da última mudança de conteúdo
(cards/<projeto>.export_state.json). Cartões não alterados aqui mantêm a data antiga e não
apagam edições feitas dentro do Anki. O mesmo vale para os modelos: MODELS_MTIME só deve
ser aumentado quando os templates ou o CSS mudarem.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from anki.buildinfo import version as ANKI_VERSION
from anki.collection import Collection, ExportAnkiPackageOptions, NoteIdsLimit
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_apkg import verify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CARD_TYPES = {"basic", "cloze", "image_occlusion", "clinical_reasoning"}
REQUIRED = ("card_id", "card_type", "source_file", "source_version", "topic", "priority", "validation_status")

# IDs fixos: mudar qualquer um deles faz o Anki tratar os modelos como novos e gera
# conflito ao reimportar baralhos já estudados.
NT_BASIC, NT_CLOZE, NT_IO = 1763136000001, 1763136000002, 1763136000003
MODELS_MTIME = 1763136000  # 2025-11-14; versão atual dos templates e do CSS
MODELS = {
    "basic": ("MedElite Basic", NT_BASIC, [7101, 7102, 7103, 7104], [7191]),
    "cloze": ("MedElite Cloze", NT_CLOZE, [7201, 7202, 7203], [7291]),
    "io": ("MedElite Image Occlusion", NT_IO, [7301, 7302, 7303, 7304, 7305], [7391]),
}

CSS = """.card {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 20px;
  line-height: 1.45;
  text-align: center;
  max-width: 40em;
  margin: 0 auto;
  padding: 0.6em;
}
img { max-width: 100%; height: auto; margin: 0.5em 0; }
ul, ol { display: inline-block; text-align: left; margin: 0.3em 0; padding-left: 1.3em; }
.extra { margin-top: 0.9em; font-size: 0.85em; opacity: 0.85; }
.source { margin-top: 1.4em; font-size: 0.62em; opacity: 0.55; }
.cloze { font-weight: bold; color: #1d4ed8; }
.nightMode .cloze { color: #93c5fd; }
"""
TAIL = (
    "{{#Extra}}<div class=\"extra\">{{Extra}}</div>{{/Extra}}"
    "{{#Source}}<div class=\"source\">{{Source}}</div>{{/Source}}"
)


class ExportError(Exception):
    pass


# ---------- normalização ----------

STOP = set("""
a o as os um uma uns umas de do da dos das em no na nos nas por para com sem sob sobre entre e ou
mas que se como quando onde qual quais quem seu sua seus suas este esta esse essa isto isso aquele
aquela ao aos pelo pela pelos pelas mais menos muito pouco ser estar ter haver sao foi era sera
pode podem deve devem tem cada todo toda todos todas outro outra outros outras tambem ja nao sim
apenas ainda assim entao pois porque caso desde ate apos antes durante contra segundo conforme
sendo tendo estao foram seja sejam qualquer mesmo mesma tipo forma
verdadeiro verdadeira falso falsa correto correta incorreto incorreta ambos ambas nenhum nenhuma
exceto respectivamente
""".split())
QUESTION = set("""
quanto quantos quantas defina definicao cite descreva indique explique diferencie diferenca
caracteriza caracteristica caracteristico principal principais complete lacuna significa
representa indica sugere esperado comum comuns frequente frequentes
""".split())
MAX_BACK, MAX_FRONT, MAX_GAPS = 25, 30, 3
ACRONYM = re.compile(r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9]{1,2}\b")


def loose(text: str) -> str:
    """Chave de comparação tolerante a quebras de linha, hifenização, caixa e ligaduras."""
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    text = text.translate(str.maketrans("“”‘’", "\"\"''"))
    return re.sub(r"[\s\-‐‑‒–—]+", "", text).casefold()


def fold(text: str) -> str:
    """Sem acento, em minúsculas e com a hifenização de fim de linha desfeita."""
    text = unicodedata.normalize("NFKC", text or "").replace("­", "")
    text = re.sub(r"(\w)[-‐‑]\s*\n\s*(\w)", r"\1\2", text)
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn").casefold()


def strip_markup(text: str) -> str:
    text = re.sub(r"\{\{c\d+::(.*?)(?:::.*?)?\}\}", r"\1", text or "", flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def visible(card: dict) -> str:
    """Texto que o estudante vê, sem HTML nem marcação de lacuna."""
    fields = {
        "cloze": ("text", "extra"),
        "image_occlusion": ("header", "back_extra"),
    }.get(card["card_type"], ("front", "back", "extra"))
    return strip_markup(" ".join(str(card.get(f) or "") for f in fields))


def answer_of(card: dict) -> str:
    """O que o cartão afirma: verso e extra; no cloze, a frase inteira."""
    fields = ("text", "extra") if card["card_type"] == "cloze" else ("back", "extra")
    return " ".join(str(card.get(f) or "") for f in fields)


def numbers(text: str) -> set[str]:
    return {m.group().replace(",", ".") for m in re.finditer(r"\d+(?:[.,]\d+)*", text)}


def count_words(text: str) -> int:
    return len(re.findall(r"[a-z0-9]+", fold(strip_markup(text))))


def terms(text: str, skip: set[str] = frozenset()) -> list[str]:
    """Palavras de conteúdo (4+ letras) e siglas em maiúsculas (2-3 letras) de um campo do cartão."""
    plain = strip_markup(text)
    acronyms = {fold(a) for a in ACRONYM.findall(plain)}
    out = []
    for word in re.findall(r"[a-z0-9]+", fold(plain)):
        if word.isdigit() or word in STOP or word in skip or word in out:
            continue
        if len(word) >= 4 or word in acronyms:
            out.append(word)
    return out


class Material:
    """Vocabulário das fontes. Compara por radical para aceitar flexões (tratar/tratamento)."""

    def __init__(self, texts):
        self.tokens: set[str] = set()
        self.prefixes: set[str] = set()
        for text in texts:
            for word in re.findall(r"[a-z0-9]+", fold(text)):
                if word not in self.tokens:
                    self.tokens.add(word)
                    self.prefixes.update(word[:n] for n in range(4, len(word) + 1))

    def has(self, word: str) -> bool:
        return word in self.tokens if len(word) < 4 else word[:max(4, len(word) - 2)] in self.prefixes


def pages_of(value) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [int(v) for v in value]
    m = re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", str(value))
    return list(range(int(m[1]), int(m[2]) + 1)) if m else [int(value)]


def load_sources(deck: dict, root: Path) -> dict:
    sources = {}
    for src in deck.get("sources", []):
        units = None
        if src.get("source_text"):
            data = json.loads((root / src["source_text"]).read_text("utf-8"))
            if data["source_version"] != src["source_version"]:
                raise ExportError(f"{src['source_file']}: o texto extraído é de outra versão do arquivo")
            units = data["units"]
        sources[src["source_file"]] = {"version": src["source_version"], "units": units}
    if not sources:
        raise ExportError("o arquivo de cartões não declara nenhuma fonte autorizada em 'sources'")
    return sources


def cited_units(card: dict, units: list[dict]) -> list[dict]:
    pages = pages_of(card.get("source_page"))
    if pages:
        return [u for u in units if u.get("page") in pages]
    section = card.get("source_section")
    return [u for u in units if section and u.get("section") == section] or units


# ---------- travas ----------

def check_card(card: dict, sources: dict, material: Material, root: Path) -> tuple[list[str], list[str]]:
    """Devolve (erros, avisos). Erro cancela a exportação; aviso é relatado ao usuário."""
    errors = [f"metadado ausente: {k}" for k in REQUIRED if card.get(k) in (None, "")]
    warnings: list[str] = []
    ctype = card.get("card_type")
    if ctype not in CARD_TYPES:
        return errors + [f"card_type inválido: {ctype}"], warnings
    if not card.get("source_page") and not card.get("source_section"):
        errors.append("metadado ausente: source_page ou source_section")
    if not isinstance(card.get("priority"), (int, float)) or not 0 <= card["priority"] <= 100:
        errors.append("priority deve ser um número de 0 a 100")

    # estrutura e forma
    items = len(re.findall(r"<li\b", str(card.get("back") or ""), re.I))
    if ctype in ("basic", "clinical_reasoning"):
        front, back = str(card.get("front") or ""), str(card.get("back") or "")
        errors += [f"campo vazio: {f}" for f, v in (("front", front), ("back", back)) if not strip_markup(v).strip()]
        if fold(strip_markup(front)).strip() == fold(strip_markup(back)).strip():
            errors.append("frente e verso iguais")
        if count_words(back) > MAX_BACK:
            warnings.append(f"verso com {count_words(back)} palavras (limite {MAX_BACK}): fragmentar ou encurtar")
        if count_words(front) > MAX_FRONT:
            warnings.append(f"frente com {count_words(front)} palavras (limite {MAX_FRONT})")
        if items >= 2 and str(items) not in {n.lstrip("0") for n in numbers(strip_markup(front))}:
            warnings.append(f"lista de {items} itens sem a quantidade na frente")
    elif ctype == "cloze":
        gaps = re.findall(r"\{\{c(\d+)::(.*?)(?:::.*?)?\}\}", card.get("text") or "", re.S)
        ords = sorted({int(n) for n, _ in gaps})
        if not ords or ords != list(range(1, len(ords) + 1)):
            errors.append("cloze sem lacunas válidas numeradas a partir de c1")
        if any(not strip_markup(g).strip() for _, g in gaps):
            errors.append("lacuna vazia")
        if not terms(re.sub(r"\{\{c\d+::.*?\}\}", " ", card.get("text") or "", flags=re.S)):
            errors.append("a frase não deixa contexto fora das lacunas")
        if len(ords) > MAX_GAPS:
            warnings.append(f"{len(ords)} lacunas no mesmo cartão (limite {MAX_GAPS})")
    else:
        errors += check_occlusions(card, root)
    for name in re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", " ".join(str(v) for v in card.values())):
        if name not in {Path(m).name for m in card.get("media", [])}:
            errors.append(f"imagem referenciada sem arquivo em 'media': {name}")
    errors += [f"mídia inexistente: {m}" for m in card.get("media", []) if not (root / m).is_file()]

    # fidelidade à fonte
    src = sources.get(card.get("source_file"))
    if src is None:
        return errors + ["source_file não está entre as fontes autorizadas desta geração"], warnings
    if card.get("source_version") != src["version"]:
        errors.append("source_version não corresponde à fonte autorizada")
    if card.get("evidence_kind") == "visual":
        if not card.get("evidence_image") or not (root / card["evidence_image"]).is_file():
            errors.append("evidência visual sem evidence_image existente")
        return errors, warnings
    if src["units"] is None:
        return errors + ["fonte sem texto extraído: use evidence_kind 'visual' com evidence_image"], warnings

    units = cited_units(card, src["units"])
    page_text = "\n".join(u["text"] for u in units)
    excerpts = card.get("evidence_excerpt")
    excerpts = [excerpts] if isinstance(excerpts, str) else list(excerpts or [""])
    found = []
    for excerpt in excerpts:
        key = loose(str(excerpt))
        if len(key) < 12:
            errors.append("evidence_excerpt ausente ou curto demais")
        elif key in loose(page_text):
            found.append(excerpt)
        else:
            where = [u.get("page") or u.get("section") for u in src["units"] if key in loose(u["text"])]
            hint = f" (está em {where[0]})" if where else ""
            errors.append(f"evidence_excerpt não encontrado na página indicada da fonte{hint}")
    if any(u.get("origin") == "visual_transcription" for u in units):
        warnings.append("evidência vem de transcrição visual: conferir no original")
    if len(found) != len(excerpts):
        return errors, warnings

    # números: todos precisam estar nos trechos de evidência
    allowed = numbers(" ".join(found))
    if items >= 2:
        allowed |= {str(items), f"{items:02d}"}  # "Quais são as 3 ...?" conta os itens do verso
    missing = numbers(visible(card)) - allowed
    if missing:
        errors.append(f"número(s) sem respaldo no trecho de evidência: {', '.join(sorted(missing))}")

    # termos: o que o cartão afirma precisa existir nas fontes autorizadas
    answer = terms(answer_of(card))
    absent = [w for w in answer if not material.has(w)]
    acronyms = [w for w in absent if len(w) < 4]
    if len(absent) > len(acronyms):
        errors.append("termo(s) da resposta ausentes das fontes autorizadas: "
                      + ", ".join(w for w in absent if len(w) >= 4))
    if acronyms:
        warnings.append(f"sigla(s) que a fonte não usa: {', '.join(w.upper() for w in acronyms)}")
    page = Material([page_text])
    off_page = [w for w in answer if w not in absent and not page.has(w)]
    if off_page:
        warnings.append(f"termo(s) da resposta fora da página citada: {', '.join(off_page)}")
    if ctype in ("basic", "clinical_reasoning"):
        unknown = [w for w in terms(card.get("front") or "", QUESTION) if not material.has(w)]
        if unknown:
            label = "da vinheta" if ctype == "clinical_reasoning" else "da pergunta"
            warnings.append(f"termo(s) {label} ausentes das fontes: {', '.join(unknown)}")
    return errors, warnings


def check_occlusions(card: dict, root: Path) -> list[str]:
    image = root / str(card.get("image") or "")
    if not image.is_file():
        return [f"imagem inexistente: {card.get('image')}"]
    if card.get("occlusion_mode") not in ("hide_all_guess_one", "hide_one_guess_one"):
        return ["occlusion_mode deve ser hide_all_guess_one ou hide_one_guess_one"]
    if not card.get("occlusions"):
        return ["image_occlusion sem máscaras"]
    try:
        occlusion_string(card, root)
    except (ExportError, KeyError, TypeError, ValueError) as exc:
        return [f"máscara inválida: {exc}"]
    return []


# ---------- image occlusion nativo ----------

def _n(value: float) -> str:
    """Formato do Anki: fração de 0 a 1, até 4 casas, sem zero à esquerda."""
    if not -1e-9 <= value <= 1 + 1e-9:
        raise ExportError("coordenada fora da imagem")
    out = f"{min(max(value, 0), 1):.4f}".rstrip("0").rstrip(".")
    return out[1:] if out.startswith("0.") else out


def occlusion_string(card: dict, root: Path) -> str:
    """Converte máscaras em pixels para o campo Occlusion do Image Occlusion nativo."""
    with Image.open(root / card["image"]) as im:
        width, height = im.size
    suffix = ":oi=1" if card["occlusion_mode"] == "hide_all_guess_one" else ""
    parts = []
    for i, occ in enumerate(card["occlusions"], 1):
        shape = occ["shape"]
        if shape == "polygon":
            if len(occ["points"]) < 3:
                raise ExportError("polígono com menos de 3 pontos")
            props = "points=" + " ".join(f"{_n(x / width)},{_n(y / height)}" for x, y in occ["points"])
        elif shape in ("rect", "ellipse"):
            x, y, w, h = (float(occ[k]) for k in ("x", "y", "w", "h"))
            if w <= 0 or h <= 0:
                raise ExportError("máscara sem área")
            _n((x + w) / width), _n((y + h) / height)
            if shape == "rect":
                props = f"left={_n(x / width)}:top={_n(y / height)}:width={_n(w / width)}:height={_n(h / height)}"
            else:
                props = f"left={_n(x / width)}:top={_n(y / height)}:rx={_n(w / 2 / width)}:ry={_n(h / 2 / height)}"
        else:
            raise ExportError(f"forma não suportada: {shape}")
        parts.append(f"{{{{c{int(occ.get('group', i))}::image-occlusion:{shape}:{props}{suffix}}}}}")
    return "<br>".join(parts)


# ---------- construção da coleção ----------

def _pin(col: Collection, path: str, name: str, key: str) -> Collection:
    """Fixa nome e IDs de um modelo. A biblioteca não aceita ID de modelo na criação."""
    new_name, ntid, field_ids, tmpl_ids = MODELS[key]
    nt = col.models.by_name(name)
    nt["name"] = new_name
    for field, fid in zip(nt["flds"], field_ids, strict=True):
        field["id"] = fid
    for tmpl, tid in zip(nt["tmpls"], tmpl_ids, strict=True):
        tmpl["id"] = tid
    col.models.update_dict(nt)
    for sql in (
        "update notetypes set id=? where id=?",
        "update fields set ntid=? where ntid=?",
        "update templates set ntid=? where ntid=?",
    ):
        col.db.execute(sql, ntid, nt["id"])
    col.db.execute("update notetypes set mtime_secs=? where id=?", MODELS_MTIME, ntid)
    col.close()
    return Collection(path)


def _new_model(col: Collection, name: str, fields: tuple[str, ...], qfmt: str, afmt: str, cloze: bool) -> None:
    nt = col.models.new(name)
    if cloze:
        nt["type"] = 1
    for field in fields:
        col.models.add_field(nt, col.models.new_field(field))
    tmpl = col.models.new_template("Cloze" if cloze else "Card 1")
    tmpl["qfmt"], tmpl["afmt"] = qfmt, afmt
    col.models.add_template(nt, tmpl)
    nt["css"] = CSS
    col.models.add_dict(nt)


def open_collection(path: str) -> Collection:
    col = Collection(path)
    _new_model(
        col, "tmp basic", ("Front", "Back", "Extra", "Source"),
        "<div class=\"front\">{{Front}}</div>",
        "{{FrontSide}}<hr id=\"answer\"><div class=\"back\">{{Back}}</div>" + TAIL, cloze=False,
    )
    _new_model(
        col, "tmp cloze", ("Text", "Extra", "Source"),
        "<div class=\"front\">{{cloze:Text}}</div>",
        "<div class=\"front\">{{cloze:Text}}</div>" + TAIL, cloze=True,
    )
    col = _pin(col, path, "tmp basic", "basic")
    col = _pin(col, path, "tmp cloze", "cloze")
    return _pin(col, path, "Image Occlusion", "io")  # modelo nativo de fábrica, só renomeado


def guid_for(project: str, card_id: str) -> str:
    return "mea-" + hashlib.sha1(f"{project}:{card_id}".encode()).hexdigest()[:20]


def source_label(card: dict) -> str:
    label = Path(card["source_file"]).name
    if card.get("source_page"):
        label += f", p. {card['source_page']}"
    if card.get("source_section"):
        label += f" — {card['source_section']}"
    return html.escape(label)


def add_card(col: Collection, card: dict, deck: dict, root: Path) -> int:
    did = col.decks.id(card.get("deck") or deck["deck"])
    tags = [re.sub(r"\s+", "_", t.strip()) for t in card.get("tags", []) if t.strip()]
    for item in card.get("media", []):
        stored = col.media.add_file(str(root / item))
        if stored != Path(item).name:
            raise ExportError(f"{card['card_id']}: nome de mídia em conflito: {Path(item).name}")
    if card["card_type"] == "image_occlusion":
        col.decks.select(did)
        before = set(col.find_notes(""))
        col.add_image_occlusion_note(
            notetype_id=NT_IO, image_path=str(root / card["image"]), occlusions=occlusion_string(card, root),
            header=card.get("header") or "", back_extra=card.get("back_extra") or "", tags=tags,
        )
        note = col.get_note((set(col.find_notes("")) - before).pop())
        note["Comments"] = source_label(card)
        note.guid = guid_for(deck["project"], card["card_id"])
        col.update_note(note)
        return note.id
    if card["card_type"] == "cloze":
        note = col.new_note(col.models.get(NT_CLOZE))
        note["Text"] = card["text"]
    else:
        note = col.new_note(col.models.get(NT_BASIC))
        note["Front"], note["Back"] = card["front"], card["back"]
    note["Extra"] = card.get("extra") or ""
    note["Source"] = source_label(card)
    note.tags = tags
    note.guid = guid_for(deck["project"], card["card_id"])
    col.add_note(note, did)
    return note.id


def stamp_notes(col: Collection, note_ids: list[int], previous: dict) -> dict:
    """Mantém a data de modificação das notas cujo conteúdo não mudou desde a última exportação."""
    state, now = {}, int(time.time())
    for nid in note_ids:
        note = col.get_note(nid)
        digest = hashlib.sha1(json.dumps([note.mid, note.fields, sorted(note.tags)]).encode()).hexdigest()
        old = previous.get(note.guid)
        mod = old["mod"] if old and old["hash"] == digest else now
        col.db.execute("update notes set mod=? where id=?", mod, nid)
        state[note.guid] = {"hash": digest, "mod": mod}
    return state


def audit(deck: dict, root: Path, statuses: tuple[str, ...] = ("approved",)) -> tuple[list, list, dict, dict]:
    """Aplica as travas aos cartões com os status indicados. Devolve (checados, pulados, erros, avisos)."""
    for key in ("project", "deck", "cards"):
        if not deck.get(key):
            raise ExportError(f"arquivo de cartões sem '{key}'")
    sources = load_sources(deck, root)
    material = Material(u["text"] for s in sources.values() for u in (s["units"] or []))
    checked, skipped, failures, warnings = [], [], {}, {}
    seen_ids, seen_fronts = set(), {}
    for card in deck["cards"]:
        cid = card.get("card_id") or "?"
        if cid in seen_ids:
            failures.setdefault(cid, []).append("card_id repetido")
        seen_ids.add(cid)
        if card.get("validation_status") not in statuses:
            skipped.append({"card_id": cid, "validation_status": card.get("validation_status")})
            continue
        errors, warns = check_card(card, sources, material, root)
        front = loose(visible(card) if card.get("card_type") == "cloze" else str(card.get("front") or cid))
        if card.get("card_type") != "image_occlusion" and front in seen_fronts:
            errors.append(f"frente idêntica à do cartão {seen_fronts[front]}")
        seen_fronts.setdefault(front, cid)
        if errors:
            failures.setdefault(cid, []).extend(errors)
        if warns:
            warnings[cid] = warns
        checked.append(card)
    return checked, skipped, failures, warnings


def draw_masks(card: dict, root: Path, out_dir: Path) -> Path:
    """Desenha as máscaras sobre a imagem, para conferir o posicionamento antes de exportar."""
    from PIL import ImageDraw

    with Image.open(root / card["image"]) as im:
        base = im.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    fill, line = (220, 38, 38, 110), (220, 38, 38, 255)
    for i, occ in enumerate(card["occlusions"], 1):
        if occ["shape"] == "polygon":
            points = [tuple(p) for p in occ["points"]]
            draw.polygon(points, fill=fill, outline=line)
            anchor = points[0]
        else:
            box = (occ["x"], occ["y"], occ["x"] + occ["w"], occ["y"] + occ["h"])
            (draw.rectangle if occ["shape"] == "rect" else draw.ellipse)(box, fill=fill, outline=line, width=2)
            anchor = box[:2]
        draw.text((anchor[0] + 4, anchor[1] + 2), str(occ.get("group", i)), fill=(255, 255, 255, 255))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{card['card_id']}.png"
    Image.alpha_composite(base, layer).convert("RGB").save(out)
    return out


def check(deck_path: Path, root: Path = ROOT) -> dict:
    """Confere cartões aprovados e em revisão sem gerar pacote. Desenha as máscaras de oclusão."""
    deck = json.loads(deck_path.read_text("utf-8"))
    checked, skipped, failures, warnings = audit(deck, root, ("approved", "needs_review"))
    masks = [
        str(draw_masks(c, root, root / "projects" / deck["project"] / "masks").relative_to(root))
        for c in checked if c.get("card_type") == "image_occlusion" and c["card_id"] not in failures
    ]
    return {"checked": len(checked), "with_errors": len(failures), "with_warnings": len(warnings),
            "errors": failures, "warnings": warnings, "mask_previews": masks, "not_checked": skipped}


def export(deck_path: Path, out: Path | None = None, modern: bool = False, root: Path = ROOT) -> dict:
    deck = json.loads(deck_path.read_text("utf-8"))
    approved, skipped, failures, warnings = audit(deck, root)
    out = out or root / "exports" / f"{deck['project']}.apkg"
    state_path = deck_path.with_suffix(".export_state.json")
    if failures:
        lines = [f"  {cid}: {'; '.join(errs)}" for cid, errs in failures.items()]
        raise ExportError("exportação cancelada; cartões aprovados com falha:\n" + "\n".join(lines))
    if not approved:
        raise ExportError("nenhum cartão com validation_status 'approved'")

    with tempfile.TemporaryDirectory() as tmp:
        col = open_collection(os.path.join(tmp, "build.anki2"))
        try:
            note_ids = [add_card(col, card, deck, root) for card in approved]
            state = stamp_notes(col, note_ids, json.loads(state_path.read_text("utf-8")) if state_path.exists() else {})
            staged = os.path.join(tmp, "out.apkg")
            col.export_anki_package(
                out_path=staged,
                options=ExportAnkiPackageOptions(
                    with_scheduling=False, with_deck_configs=False, with_media=True, legacy=not modern
                ),
                limit=NoteIdsLimit(note_ids=note_ids),
            )
        finally:
            col.close()
        guids = {guid_for(deck["project"], c["card_id"]) for c in approved}
        report = verify(staged, expect_guids=guids)
        if not report["ok"]:
            raise ExportError("o pacote não passou na verificação:\n  " + "\n  ".join(report["problems"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(staged, out)
        state_path.write_text(json.dumps(state, indent=1) + "\n", "utf-8")

    trace_keys = (
        "card_id", "card_type", "source_file", "source_page", "source_section", "evidence_excerpt",
        "evidence_kind", "evidence_image", "image", "occlusions", "topic", "priority",
        "validation_status", "source_version",
    )
    manifest = {
        "project": deck["project"],
        "apkg": str(out.relative_to(root)) if out.is_relative_to(root) else str(out),
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anki_library": ANKI_VERSION,
        "format": "moderno (Anki 2.1.50+)" if modern else "compatível com versões antigas",
        "notes": report["notes"],
        "cards": report["cards"],
        "notes_by_type": report["notes_by_type"],
        "decks": report["decks"],
        "tags": report["tags"],
        "media_files": report["media_files"],
        "image_occlusion": {"notes": report["image_occlusion_notes"], "shapes": report["image_occlusion_shapes"]},
        "scheduled_cards": report["scheduled_cards"],
        "revlog_entries": report["revlog_entries"],
        "evidence_checked_visually": [c["card_id"] for c in approved if c.get("evidence_kind") == "visual"],
        "warnings": warnings,
        "skipped": skipped,
        "traceability": [
            {**{k: c[k] for k in trace_keys if k in c}, "guid": guid_for(deck["project"], c["card_id"])}
            for c in approved
        ],
    }
    out.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Exporta cartões aprovados para .apkg")
    parser.add_argument("cards", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--modern", action="store_true", help="formato novo do Anki (2.1.50+), arquivo menor")
    parser.add_argument("--check", action="store_true", help="só confere (aprovados e em revisão), sem exportar")
    args = parser.parse_args()
    try:
        if args.check:
            report = check(args.cards)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            sys.exit(1 if report["errors"] else 0)
        manifest = export(args.cards, args.out, args.modern)
    except ExportError as exc:
        sys.exit(f"ERRO: {exc}")
    summary = {k: manifest[k] for k in (
        "apkg", "notes", "cards", "notes_by_type", "decks", "media_files", "image_occlusion",
        "scheduled_cards", "revlog_entries", "evidence_checked_visually", "warnings",
    )}
    summary["skipped"] = len(manifest["skipped"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
