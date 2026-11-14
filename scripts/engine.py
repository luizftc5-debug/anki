#!/usr/bin/env python3
"""MEDICAL ELITE — ANKI ENGINE: validação, prévia, aprovação, exportação e feedback.

Uso:
  python scripts/engine.py validate <run>                 auditoria determinística -> runs/<run>/audit.json
  python scripts/engine.py preview  <run> [--from N --to M] gera runs/<run>/review.md; imprime a página pedida
  python scripts/engine.py approve  <run> --all | --ids 1,4,7-12
  python scripts/engine.py revoke   <run> --all | --ids ...
  python scripts/engine.py status   <run>
  python scripts/engine.py export   <run>                 CSV + .apkg, só com cartões aprovados e inalterados
  python scripts/engine.py feedback --problem ... --correction ... --kind ... --scope local|geral [...]

<run> é o slug em runs/<run>/. Os cartões ficam em runs/<run>/candidates.jsonl (um JSON por linha,
schema em rules/ANKI_EXPORT.md). A redação e a auditoria semântica são feitas pelo modelo; este
script faz só o que é determinístico: contar, comparar, conferir evidência, empacotar e testar.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATES = ("aprovado", "reescrever", "revisao_cientifica", "descartado")
PRIORITIES = ("A", "B", "C")
VERIFICATION = ("fonte_primaria", "verificado_externo", "pendente")
CONTENT_KEYS = ("front", "back", "tag", "image", "image_side")


# ---------------------------------------------------------------- utilidades de texto

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plain(text: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</li>|</p>|</div>", " ", text or "")
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def words(text: str) -> int:
    return len(plain(text).split())


def fold(text: str) -> str:
    """Normaliza para comparação literal: NFKC, hifenização de quebra de linha, espaços, caixa."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = text.replace("\u00ad", "").translate(str.maketrans("“”‘’–—", "\"\"''--"))
    return re.sub(r"\s+", " ", text).strip().casefold()


def deaccent(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", text)}


def content_hash(card: dict) -> str:
    payload = json.dumps({k: card.get(k) for k in CONTENT_KEYS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def parse_ids(spec: str, cards: list[dict]) -> list[str]:
    """'1,4,7-9' -> ids. Aceita o id literal ('007') ou a posição (7)."""
    known = {c["id"] for c in cards}
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            nums = range(int(a), int(b) + 1)
        else:
            nums = [part]
        for n in nums:
            n = str(n)
            match = n if n in known else next((c["id"] for c in cards if c["id"].lstrip("0") == n.lstrip("0")), None)
            if match is None:
                sys.exit(f"ERRO: cartão inexistente: {n}")
            out.append(match)
    return out


# ---------------------------------------------------------------- leitura do run

def settings(root: Path = ROOT) -> dict:
    return json.loads((root / "config" / "settings.json").read_text("utf-8"))


def run_dir(name: str, root: Path = ROOT) -> Path:
    path = root / "runs" / name
    if not (path / "candidates.jsonl").is_file():
        sys.exit(f"ERRO: {path / 'candidates.jsonl'} não existe")
    return path


def load_cards(run: Path) -> list[dict]:
    cards = []
    for n, line in enumerate((run / "candidates.jsonl").read_text("utf-8").splitlines(), 1):
        if line.strip():
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError as exc:
                sys.exit(f"ERRO: candidates.jsonl linha {n}: {exc}")
    return cards


def load_json(path: Path, default):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")


def load_sources(run: Path) -> list[dict]:
    return load_json(run / "source_text.json", {"sources": []})["sources"]


def pages_of(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [p for v in value for p in pages_of(v)]
    if isinstance(value, str) and re.fullmatch(r"\d+\s*-\s*\d+", value):
        a, b = (int(x) for x in value.split("-"))
        return list(range(a, b + 1))
    return [int(value)] if str(value).isdigit() else [value]


def cited_text(card: dict, sources: list[dict]) -> tuple[str, str]:
    """(texto das unidades citadas, texto integral das fontes do cartão)."""
    ref = card.get("source_ref") or {}
    pool = [s for s in sources if not ref.get("file") or s["file"] == ref["file"]]
    full = "\n".join(u["text"] for s in pool for u in s["units"])
    pages, section = pages_of(ref.get("page")), ref.get("section")
    units = [u for s in pool for u in s["units"]
             if (pages and u["page"] in pages) or (section and not pages and u["section"] == section)]
    return ("\n".join(u["text"] for u in units) if units else ""), full


# ---------------------------------------------------------------- auditoria determinística

def audit_card(card: dict, sources: list[dict], cfg: dict, root: Path) -> tuple[list[str], list[str]]:
    lim = cfg["limits"]
    err, warn = [], []
    front, back = card.get("front", ""), card.get("back", "")
    pf, pb = plain(front), plain(back)

    if not pf:
        err.append("frente vazia")
    if not pb:
        err.append("verso vazio")
    if pf and fold(pf) == fold(pb):
        err.append("frente igual ao verso")
    fw, bw = words(front), words(back)
    if fw > lim["front_max_words"]:
        err.append(f"frente com {fw} palavras (máx. {lim['front_max_words']})")
    elif fw > lim["front_ideal_max_words"]:
        warn.append(f"frente com {fw} palavras (ideal até {lim['front_ideal_max_words']})")
    if bw > lim["back_hard_max_words"] and not card.get("long_answer_reason"):
        err.append(f"verso com {bw} palavras sem long_answer_reason (máx. {lim['back_hard_max_words']})")
    elif bw > lim["back_ideal_max_words"]:
        warn.append(f"verso com {bw} palavras (ideal até {lim['back_ideal_max_words']})")
    if pf.count("?") > 1:
        err.append("frente com mais de uma pergunta")
    if re.search(r"\?\s*\S.*\?", pf) or re.search(r"(?i)\?\s*(e|ou)\s+(qual|quais|como|quando|por que)\b", pf):
        err.append("frente com perguntas encadeadas")
    if re.search(r"(?i)<li>|;|\n\s*[-•*]|(^|\s)\d+[.)]\s", back) or pb.count(",") >= 2:
        warn.append("verso parece lista ou múltiplos fatos: confirmar unidade única")
    for bad in cfg["forbidden_patterns"]:
        if re.search(bad["regex"], pf + " " + pb, re.I):
            err.append(f"padrão proibido: {bad['why']}")

    tag = card.get("tag", "")
    if not re.fullmatch(cfg["tag_regex"], tag or ""):
        err.append(f"tag inválida '{tag}' (ÁREA::SUBÁREA::ASSUNTO, ASCII, sem espaços, iniciais maiúsculas)")
    if card.get("gate") not in GATES:
        err.append(f"gate deve ser um de {GATES}")
    if card.get("priority") not in PRIORITIES:
        err.append("priority deve ser A, B ou C")
    elif card["priority"] == "C" and card.get("gate") == "aprovado":
        warn.append("prioridade C (baixo retorno) aprovada: justificar em note")
    if card.get("verification_status") not in VERIFICATION:
        err.append(f"verification_status deve ser um de {VERIFICATION}")

    if card.get("image"):
        if not (root / card["image"]).is_file():
            err.append(f"imagem não encontrada: {card['image']}")
        if card.get("image_side") not in ("front", "back"):
            err.append("image_side deve ser front ou back")

    # rastreabilidade e evidência
    evidence = card.get("evidence") or []
    evidence = [evidence] if isinstance(evidence, str) else evidence
    externals = card.get("external_refs") or []
    ref = card.get("source_ref") or {}
    if card.get("gate") == "descartado":
        return err, warn
    if not evidence and not externals:
        err.append("sem evidência: preencher evidence (trecho literal) ou external_refs")
    if evidence and not (ref.get("file") and (ref.get("page") is not None or ref.get("section"))):
        err.append("source_ref incompleto: file + page (ou section)")
    if evidence:
        local, full = cited_text(card, sources)
        if not full:
            err.append(f"fonte '{ref.get('file')}' não extraída neste run")
        for excerpt in evidence:
            if fold(excerpt) in fold(local):
                continue
            if fold(excerpt) in fold(full):
                warn.append(f"trecho existe na fonte, mas fora da página/seção citada: “{excerpt[:50]}”")
            else:
                err.append(f"trecho não encontrado literalmente na fonte: “{excerpt[:60]}”")
        if full:
            source_words = set(re.findall(r"\w{5,}", deaccent(fold(full))))
            missing = sorted({w for w in re.findall(r"\w{5,}", deaccent(fold(pb)))
                              if w not in source_words and not any(w[:5] == s[:5] for s in source_words)})
            if missing and not externals:
                warn.append("termo do verso ausente da fonte: " + ", ".join(missing[:6]))
    for ext in externals:
        if not (ext.get("ref") and ext.get("excerpt")):
            err.append("external_refs exige ref (obra, edição/ano, página) e excerpt")
    if externals and card.get("verification_status") == "fonte_primaria":
        err.append("cartão com external_refs não pode ter verification_status fonte_primaria")
    support = " ".join(evidence + [e.get("excerpt", "") for e in externals])
    loose = numbers(pf + " " + pb) - numbers(support)
    if loose:
        err.append("número sem sustentação nos trechos: " + ", ".join(sorted(loose)))
    if card.get("verification_status") == "pendente" and card.get("gate") == "aprovado":
        err.append("verification_status pendente não pode ter gate aprovado")
    return err, warn


def audit(run: Path, root: Path = ROOT) -> dict:
    cfg = settings(root)
    cards = load_cards(run)
    sources = load_sources(run)
    result, seen_ids, fronts = {}, set(), {}
    active = [c for c in cards if c.get("gate") != "descartado"]
    for c in cards:
        cid = c.get("id")
        err, warn = audit_card(c, sources, cfg, root)
        if not cid or cid in seen_ids:
            err.append(f"id ausente ou repetido: {cid}")
        seen_ids.add(cid)
        result[cid] = {"errors": err, "warnings": warn, "hash": content_hash(c), "gate": c.get("gate")}
    # duplicações entre cartões ativos
    for i, a in enumerate(active):
        key = fold(plain(a.get("front", "")))
        if key in fronts:
            result[a["id"]]["errors"].append(f"frente idêntica ao cartão {fronts[key]}")
        fronts.setdefault(key, a["id"])
        for b in active[i + 1:]:
            ra = SequenceMatcher(None, key, fold(plain(b.get("front", "")))).ratio()
            rb = SequenceMatcher(None, fold(plain(a.get("back", ""))), fold(plain(b.get("back", "")))).ratio()
            if ra >= cfg["limits"]["near_duplicate_ratio"] and rb >= 0.6:
                result[b["id"]]["warnings"].append(f"possível duplicata semântica de {a['id']}")
    # tags equivalentes com grafias diferentes
    tags = {c.get("tag", "") for c in active}
    by_key = {}
    for t in tags:
        by_key.setdefault(deaccent(t).lower(), set()).add(t)
    clash = [sorted(v) for v in by_key.values() if len(v) > 1]
    report = {"generated_at": now(), "cards": result, "tag_conflicts": clash}
    write_json(run / "audit.json", report)
    return report


# ---------------------------------------------------------------- aprovação

def approval_state(run: Path, cards: list[dict]) -> dict[str, str]:
    """id -> aprovado | alterado_apos_aprovacao | pendente | descartado."""
    approved = load_json(run / "approval.json", {"approved": {}})["approved"]
    state = {}
    for c in cards:
        if c.get("gate") == "descartado":
            state[c["id"]] = "descartado"
        elif c["id"] not in approved:
            state[c["id"]] = "pendente"
        elif approved[c["id"]]["hash"] == content_hash(c):
            state[c["id"]] = "aprovado"
        else:
            state[c["id"]] = "alterado_apos_aprovacao"
    return state


def approve(run: Path, ids: list[str] | None, root: Path = ROOT) -> dict:
    cards = load_cards(run)
    report = audit(run, root)
    # --all = todos os cartões com gate "aprovado"; os em reescrita/revisão científica ficam pendentes
    targets = [c for c in cards if c.get("gate") == "aprovado"] if ids is None else \
        [c for c in cards if c["id"] in ids]
    left_out = [c["id"] for c in cards if ids is None and c.get("gate") in ("reescrever", "revisao_cientifica")]
    blocked = {c["id"]: report["cards"][c["id"]]["errors"] or [f"gate {c.get('gate')}"]
               for c in targets if report["cards"][c["id"]]["errors"] or c.get("gate") != "aprovado"}
    if blocked:
        lines = "\n".join(f"  {k}: {'; '.join(v)}" for k, v in blocked.items())
        raise SystemExit(f"ERRO: aprovação recusada; nada foi registrado:\n{lines}")
    path = run / "approval.json"
    data = load_json(path, {"approved": {}, "log": []})
    stamp = now()
    for c in targets:
        data["approved"][c["id"]] = {"hash": content_hash(c), "at": stamp}
    data["log"].append({"at": stamp, "action": "approve", "ids": [c["id"] for c in targets]})
    write_json(path, data)
    return {"aprovados": len(targets), "nao_aprovaveis_ainda": left_out}


def revoke(run: Path, ids: list[str] | None) -> dict:
    path = run / "approval.json"
    data = load_json(path, {"approved": {}, "log": []})
    ids = list(data["approved"]) if ids is None else ids
    for i in ids:
        data["approved"].pop(i, None)
    data["log"].append({"at": now(), "action": "revoke", "ids": ids})
    write_json(path, data)
    return {"revoked": len(ids)}


# ---------------------------------------------------------------- prévia

def md_cell(text: str) -> str:
    text = re.sub(r"<img[^>]*src=\"([^\"]+)\"[^>]*>", r"[img \1]", text or "")
    return plain(text).replace("|", "\\|").replace("\n", " ")


def summary(cards: list[dict], report: dict, state: dict) -> dict:
    by_gate = {g: sum(c.get("gate") == g for c in cards) for g in GATES}
    formal = sum(1 for c in cards if c.get("gate") == "aprovado" and not report["cards"][c["id"]]["errors"])
    areas = {}
    for c in cards:
        if c.get("gate") != "descartado":
            key = "::".join((c.get("tag") or "?").split("::")[:2])
            areas[key] = areas.get(key, 0) + 1
    return {
        "candidatos": len(cards),
        "aprovados_criterios_formais": formal,
        "reescrever": by_gate["reescrever"],
        "revisao_cientifica": by_gate["revisao_cientifica"],
        "descartados": by_gate["descartado"],
        "com_erro": sum(1 for v in report["cards"].values() if v["errors"]),
        "com_aviso": sum(1 for v in report["cards"].values() if v["warnings"]),
        "aprovados_pelo_usuario": sum(v == "aprovado" for v in state.values()),
        "alterados_apos_aprovacao": sum(v == "alterado_apos_aprovacao" for v in state.values()),
        "por_area": dict(sorted(areas.items(), key=lambda kv: -kv[1])),
        "conflitos_de_tag": report["tag_conflicts"],
    }


def preview(run: Path, first: int | None = None, last: int | None = None, root: Path = ROOT) -> str:
    cards = load_cards(run)
    report = audit(run, root)
    state = approval_state(run, cards)
    s = summary(cards, report, state)
    out = [f"# Prévia — {run.name}", "", f"Gerada em {report['generated_at']}.", "", "## Resumo", ""]
    out += [f"- {k.replace('_', ' ')}: {v}" for k, v in s.items() if not isinstance(v, (dict, list))]
    out += ["- distribuição: " + "; ".join(f"{k} {v}" for k, v in s["por_area"].items())]
    if s["conflitos_de_tag"]:
        out.append("- conflitos de tag: " + "; ".join(" / ".join(x) for x in s["conflitos_de_tag"]))
    out += ["", "## Cartões", "", "| # | ID | Frente | Verso | Tag | Gate | Aprovação |", "|---|---|---|---|---|---|---|"]
    rows = []
    for n, c in enumerate(cards, 1):
        rows.append(f"| {n} | {c['id']} | {md_cell(c.get('front'))} | {md_cell(c.get('back'))} | "
                    f"{c.get('tag', '')} | {c.get('gate')} | {state[c['id']]} |")
    out += rows
    issues = [(c["id"], report["cards"][c["id"]]) for c in cards]
    issues = [(i, r) for i, r in issues if r["errors"] or r["warnings"]]
    if issues:
        out += ["", "## Problemas", ""]
        for i, r in issues:
            out += [f"- **{i}** erro: {e}" for e in r["errors"]]
            out += [f"- {i} aviso: {w}" for w in r["warnings"]]
    (run / "review.md").write_text("\n".join(out) + "\n", "utf-8")
    if first is None:
        return json.dumps(s, ensure_ascii=False, indent=1)
    page = rows[max(first, 1) - 1:last or len(rows)]
    return "\n".join(out[out.index("| # | ID | Frente | Verso | Tag | Gate | Aprovação |"):][:2] + page)


# ---------------------------------------------------------------- exportação

def note_html(card: dict, side: str, media_names: dict) -> str:
    text = card[side]
    if card.get("image") and card.get("image_side") == side:
        text += f'<br><img src="{html.escape(media_names[card["image"]])}">'
    return text


def export(run: Path, root: Path = ROOT) -> dict:
    import genanki

    cfg = settings(root)
    cards = load_cards(run)
    report = audit(run, root)
    state = approval_state(run, cards)
    changed = [i for i, v in state.items() if v == "alterado_apos_aprovacao"]
    if changed:
        raise SystemExit("ERRO: cartões alterados após a aprovação; reaprovar antes de exportar: " + ", ".join(changed))
    chosen = [c for c in cards if state[c["id"]] == "aprovado"]
    if not chosen:
        raise SystemExit("ERRO: nenhum cartão aprovado pelo usuário; exportação bloqueada")
    bad = {c["id"]: report["cards"][c["id"]]["errors"] for c in chosen if report["cards"][c["id"]]["errors"]}
    if bad:
        raise SystemExit("ERRO: cartões aprovados com erro de auditoria: " + json.dumps(bad, ensure_ascii=False))

    meta = load_json(run / "run.json", {})
    deck_name = meta.get("deck") or f"{cfg['deck_root']}::{run.name}"
    media_names = {}
    for c in chosen:
        if c.get("image") and c["image"] not in media_names:
            src = Path(c["image"])
            media_names[c["image"]] = f"{run.name}_{src.name}"

    # CSV — exatamente Frente, Verso, Tag
    csv_path = root / "exports" / "csv" / f"{run.name}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [(note_html(c, "front", media_names), note_html(c, "back", media_names), c["tag"]) for c in chosen]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(("Frente", "Verso", "Tag"))
        writer.writerows(rows)
    with csv_path.open(encoding="utf-8", newline="") as fh:
        back_rows = list(csv.reader(fh))
    if back_rows[0] != ["Frente", "Verso", "Tag"] or [tuple(r) for r in back_rows[1:]] != rows \
            or any(len(r) != 3 for r in back_rows):
        raise SystemExit("ERRO: leitura de retorno do CSV não confere")

    # .apkg — genanki, IDs estáveis
    model = genanki.Model(
        cfg["anki"]["model_id"], cfg["anki"]["model_name"],
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[{"name": "Card 1", "qfmt": "{{Front}}",
                    "afmt": "{{FrontSide}}<hr id=answer>{{Back}}"}],
        css=cfg["anki"]["css"],
    )
    deck_id = int(hashlib.sha256(deck_name.encode()).hexdigest()[:8], 16) | (1 << 30)
    deck = genanki.Deck(deck_id, deck_name)
    for c, (front, back, tag) in zip(chosen, rows):
        deck.add_note(genanki.Note(model=model, fields=[front, back], tags=[tag],
                                   guid=genanki.guid_for(run.name, c["id"])))
    apkg_path = root / "exports" / "apkg" / f"{run.name}.apkg"
    apkg_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for original, name in media_names.items():
            shutil.copyfile(root / original, Path(tmp) / name)
            files.append(str(Path(tmp) / name))
        genanki.Package(deck, media_files=files).write_to_file(str(apkg_path))
    checks = verify_apkg(apkg_path, chosen, rows, media_names, deck_name, run.name)

    write_json(run / "export.json", {"at": now(), "csv": str(csv_path.relative_to(root)),
                                     "apkg": str(apkg_path.relative_to(root)), "notes": len(chosen),
                                     "ids": [c["id"] for c in chosen], "checks": checks})
    skipped = [i for i, v in state.items() if v == "pendente"]
    return {"csv": str(csv_path.relative_to(root)), "apkg": str(apkg_path.relative_to(root)),
            "exportados": len(chosen), "nao_exportados_pendentes": skipped, "verificacao": checks}


def verify_apkg(path: Path, chosen: list[dict], rows: list[tuple], media_names: dict, deck_name: str,
                run_name: str) -> dict:
    """Integridade estrutural + importação real numa coleção Anki descartável (backend oficial)."""
    import genanki
    from anki.collection import Collection, ImportAnkiPackageRequest

    checks = {}
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit("ERRO: .apkg ausente ou vazio")
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise SystemExit("ERRO: .apkg com ZIP corrompido")
        names = set(z.namelist())
        if not ({"collection.anki2", "collection.anki21"} & names) or "media" not in names:
            raise SystemExit(f"ERRO: estrutura interna inesperada: {sorted(names)}")
        media_map = json.loads(z.read("media"))
    if sorted(media_map.values()) != sorted(media_names.values()):
        raise SystemExit("ERRO: mídia no pacote não confere")
    checks["zip_e_estrutura"] = "ok"

    expected = {genanki.guid_for(run_name, c["id"]): (r[0], r[1], c["tag"]) for c, r in zip(chosen, rows)}
    with tempfile.TemporaryDirectory() as tmp:
        col = Collection(str(Path(tmp) / "test.anki2"))
        try:
            for attempt in (1, 2):  # a 2ª importação prova que não há duplicação
                col.import_anki_package(ImportAnkiPackageRequest(package_path=str(path)))
                nids = col.find_notes("")
                if len(nids) != len(chosen) or col.card_count() != len(chosen):
                    raise SystemExit(f"ERRO: importação {attempt}: {len(nids)} notas, esperado {len(chosen)}")
            for nid in nids:
                note = col.get_note(nid)
                exp = expected.get(note.guid)
                if exp is None or [note["Front"], note["Back"]] != list(exp[:2]) or note.tags != [exp[2]]:
                    raise SystemExit(f"ERRO: nota importada não confere: {note.guid}")
            decks = [d.name for d in col.decks.all_names_and_ids()]
            if deck_name not in decks:
                raise SystemExit(f"ERRO: baralho {deck_name} ausente após importação")
            media_dir = Path(col.media.dir())
            missing = [n for n in media_names.values() if not (media_dir / n).is_file()]
            if missing:
                raise SystemExit(f"ERRO: mídia ausente após importação: {missing}")
        finally:
            col.close()
    checks["importacao_backend_anki"] = f"ok: {len(chosen)} notas, campos, tags, baralho e mídia conferidos"
    checks["reimportacao_sem_duplicar"] = "ok"
    checks["limite"] = "importação testada com o backend oficial (pacote anki); não testada na interface do Anki Desktop/AnkiDroid/AnkiMobile"
    return checks


# ---------------------------------------------------------------- feedback

def feedback(args, root: Path = ROOT) -> dict:
    entry = {"at": now(), "run": args.run, "card": args.card, "problem": args.problem,
             "correction": args.correction, "solution": args.solution, "kind": args.kind,
             "scope": args.scope, "rule": args.rule}
    path = root / "data" / "feedback.jsonl"
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if args.scope == "geral":
        if not args.rule:
            raise SystemExit("ERRO: feedback geral exige --rule")
        cfg_path = root / "config" / "settings.json"
        cfg = json.loads(cfg_path.read_text("utf-8"))
        with (root / "data" / "settings_history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": entry["at"], "settings": cfg}, ensure_ascii=False) + "\n")
        cfg["learned_rules"].append({"rule": args.rule, "kind": args.kind, "date": entry["at"][:10]})
        write_json(cfg_path, cfg)
    return {"registrado": True, "escopo": args.scope}


# ---------------------------------------------------------------- CLI

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "status", "export"):
        sub.add_parser(name).add_argument("run")
    pv = sub.add_parser("preview")
    pv.add_argument("run")
    pv.add_argument("--from", dest="first", type=int)
    pv.add_argument("--to", dest="last", type=int)
    for name in ("approve", "revoke"):
        sp = sub.add_parser(name)
        sp.add_argument("run")
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--all", action="store_true")
        g.add_argument("--ids")
    fb = sub.add_parser("feedback")
    fb.add_argument("--run")
    fb.add_argument("--card")
    fb.add_argument("--problem", required=True)
    fb.add_argument("--correction", required=True)
    fb.add_argument("--solution", default="")
    fb.add_argument("--kind", required=True,
                    help="atomicidade, concisao, clareza, unicidade, relevancia, precisao, tag, estilo, outro")
    fb.add_argument("--scope", choices=("local", "geral"), required=True)
    fb.add_argument("--rule", help="regra editorial generalizável (obrigatória se --scope geral)")
    args = p.parse_args()

    if args.cmd == "feedback":
        print(json.dumps(feedback(args), ensure_ascii=False))
        return
    run = run_dir(args.run)
    if args.cmd == "validate":
        r = audit(run)
        bad = {k: v for k, v in r["cards"].items() if v["errors"] or v["warnings"]}
        print(json.dumps({"cartoes": len(r["cards"]), "com_problema": bad, "conflitos_de_tag": r["tag_conflicts"]},
                         ensure_ascii=False, indent=1))
    elif args.cmd == "preview":
        print(preview(run, args.first, args.last))
        print(f"\nPrévia integral: {run.relative_to(ROOT) / 'review.md'}")
    elif args.cmd == "status":
        state = approval_state(run, load_cards(run))
        counts = {}
        for v in state.values():
            counts[v] = counts.get(v, 0) + 1
        print(json.dumps({"contagem": counts, "nao_aprovados": [k for k, v in state.items()
                                                               if v in ("pendente", "alterado_apos_aprovacao")]},
                         ensure_ascii=False))
    elif args.cmd in ("approve", "revoke"):
        cards = load_cards(run)
        ids = None if args.all else parse_ids(args.ids, cards)
        print(json.dumps(approve(run, ids) if args.cmd == "approve" else revoke(run, ids), ensure_ascii=False))
    elif args.cmd == "export":
        print(json.dumps(export(run), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
