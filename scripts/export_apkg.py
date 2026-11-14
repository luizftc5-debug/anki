#!/usr/bin/env python3
"""Exporta os cartões aprovados de cards/<projeto>.json para um .apkg real.

Uso: python scripts/export_apkg.py cards/<projeto>.json [--out exports/<projeto>.apkg] [--modern]

Usa a biblioteca oficial `anki` (o mesmo backend do Anki Desktop): modelos próprios para
Basic e Cloze e o modelo nativo de Image Occlusion (Anki 23.10+), com máscaras editáveis.

Travas antes de exportar. Qualquer falha num cartão aprovado cancela a exportação inteira:
  - metadados de rastreabilidade completos (seção 08 do CLAUDE.md);
  - o trecho de evidência precisa existir no texto extraído da fonte, na página indicada;
  - todo número visível no cartão precisa existir nessa mesma página da fonte;
  - mídia referenciada presente; lacunas Cloze válidas; máscaras dentro da imagem;
  - nenhuma frente repetida.
Cartões com validation_status diferente de "approved" não são exportados.

Estas travas são mecânicas. Elas não substituem a auditoria semântica (o trecho sustenta
mesmo a resposta?), que é feita na elaboração dos cartões.

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


# ---------- normalização e travas ----------

def loose(text: str) -> str:
    """Chave de comparação tolerante a quebras de linha, hifenização, caixa e ligaduras."""
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    text = text.translate(str.maketrans("“”‘’", "\"\"''"))
    return re.sub(r"[\s\-‐‑‒–—]+", "", text).casefold()


def visible(card: dict) -> str:
    """Texto que o estudante vê, sem HTML nem marcação de lacuna."""
    fields = {
        "cloze": ("text", "extra"),
        "image_occlusion": ("header", "back_extra"),
    }.get(card["card_type"], ("front", "back", "extra"))
    raw = " ".join(str(card.get(f) or "") for f in fields)
    raw = re.sub(r"\{\{c\d+::", "", raw).replace("}}", " ")
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def numbers(text: str) -> set[str]:
    return {m.group().replace(",", ".") for m in re.finditer(r"\d+(?:[.,]\d+)*", text)}


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


def source_text_for(card: dict, units: list[dict]) -> str:
    pages = pages_of(card.get("source_page"))
    chosen = [u for u in units if u.get("page") in pages] if pages else units
    return "\n".join(u["text"] for u in chosen)


def check_card(card: dict, sources: dict, root: Path) -> list[str]:
    errors = [f"metadado ausente: {k}" for k in REQUIRED if card.get(k) in (None, "")]
    ctype = card.get("card_type")
    if ctype not in CARD_TYPES:
        return errors + [f"card_type inválido: {ctype}"]
    if not card.get("source_page") and not card.get("source_section"):
        errors.append("metadado ausente: source_page ou source_section")
    if not isinstance(card.get("priority"), (int, float)) or not 0 <= card["priority"] <= 100:
        errors.append("priority deve ser um número de 0 a 100")

    # conteúdo
    if ctype in ("basic", "clinical_reasoning"):
        errors += [f"campo vazio: {f}" for f in ("front", "back") if not str(card.get(f) or "").strip()]
    elif ctype == "cloze":
        ords = sorted({int(n) for n in re.findall(r"\{\{c(\d+)::[^}]+\}\}", card.get("text") or "")})
        if not ords or ords != list(range(1, len(ords) + 1)):
            errors.append("cloze sem lacunas válidas numeradas a partir de c1")
    else:
        errors += check_occlusions(card, root)
    for name in re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", " ".join(str(v) for v in card.values())):
        if name not in {Path(m).name for m in card.get("media", [])}:
            errors.append(f"imagem referenciada sem arquivo em 'media': {name}")
    errors += [f"mídia inexistente: {m}" for m in card.get("media", []) if not (root / m).is_file()]

    # fidelidade à fonte
    src = sources.get(card.get("source_file"))
    if src is None:
        return errors + ["source_file não está entre as fontes autorizadas desta geração"]
    if card.get("source_version") != src["version"]:
        errors.append("source_version não corresponde à fonte autorizada")
    if card.get("evidence_kind") == "visual":
        if not card.get("evidence_image") or not (root / card["evidence_image"]).is_file():
            errors.append("evidência visual sem evidence_image existente")
    elif src["units"] is None:
        errors.append("fonte sem texto extraído: use evidence_kind 'visual' com evidence_image")
    else:
        text = source_text_for(card, src["units"])
        excerpt = str(card.get("evidence_excerpt") or "")
        if len(loose(excerpt)) < 12:
            errors.append("evidence_excerpt ausente ou curto demais")
        elif loose(excerpt) not in loose(text):
            errors.append("evidence_excerpt não encontrado na página indicada da fonte")
        missing = numbers(visible(card)) - numbers(text)
        if missing:
            errors.append(f"número(s) sem respaldo na página da fonte: {', '.join(sorted(missing))}")
    return errors


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


def export(deck_path: Path, out: Path | None = None, modern: bool = False, root: Path = ROOT) -> dict:
    deck = json.loads(deck_path.read_text("utf-8"))
    for key in ("project", "deck", "cards"):
        if not deck.get(key):
            raise ExportError(f"arquivo de cartões sem '{key}'")
    sources = load_sources(deck, root)
    out = out or root / "exports" / f"{deck['project']}.apkg"
    state_path = deck_path.with_suffix(".export_state.json")

    approved, skipped, failures = [], [], {}
    seen_ids, seen_fronts = set(), {}
    for card in deck["cards"]:
        cid = card.get("card_id") or "?"
        if cid in seen_ids:
            failures.setdefault(cid, []).append("card_id repetido")
        seen_ids.add(cid)
        if card.get("validation_status") != "approved":
            skipped.append({"card_id": cid, "validation_status": card.get("validation_status")})
            continue
        errors = check_card(card, sources, root)
        front = loose(visible(card) if card.get("card_type") == "cloze" else str(card.get("front") or cid))
        if card.get("card_type") != "image_occlusion" and front in seen_fronts:
            errors.append(f"frente idêntica à do cartão {seen_fronts[front]}")
        seen_fronts.setdefault(front, cid)
        if errors:
            failures.setdefault(cid, []).extend(errors)
        approved.append(card)
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
    args = parser.parse_args()
    try:
        manifest = export(args.cards, args.out, args.modern)
    except ExportError as exc:
        sys.exit(f"ERRO: {exc}")
    summary = {k: manifest[k] for k in (
        "apkg", "notes", "cards", "notes_by_type", "decks", "media_files", "image_occlusion",
        "scheduled_cards", "revlog_entries", "evidence_checked_visually",
    )}
    summary["skipped"] = len(manifest["skipped"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
