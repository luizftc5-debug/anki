#!/usr/bin/env python3
"""Verifica um .apkg importando-o numa coleção Anki limpa (biblioteca oficial `anki`).

Uso: python scripts/verify_apkg.py exports/<projeto>.apkg

Confere: importação sem conflito, notas e cartões gerados, ausência de cartões vazios,
mídia referenciada presente, lacunas Cloze, máscaras do Image Occlusion nativo,
renderização das faces e ausência de agendamento ou histórico de revisão.

Limite: isto exercita o mesmo backend do Anki Desktop, mas não abre a interface gráfica.
O desenho das máscaras na tela (JavaScript do revisor) não é executado aqui.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from anki.collection import Collection, ImportAnkiPackageRequest

IO_STOCK_KIND = 6  # ORIGINAL_STOCK_KIND_IMAGE_OCCLUSION
CLOZE_TYPE = 1
IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
CLOZE_ORD = re.compile(r"\{\{c(\d+)::")


def verify(apkg: str | Path, expect_guids: set[str] | None = None) -> dict:
    """Importa o pacote e devolve um relatório. `ok` é False se houver qualquer problema."""
    problems: list[str] = []
    report: dict = {"apkg": str(apkg), "problems": problems}
    with tempfile.TemporaryDirectory() as tmp:
        col = Collection(os.path.join(tmp, "verify.anki2"))
        try:
            log = col.import_anki_package(ImportAnkiPackageRequest(package_path=str(apkg))).log
            if log.conflicting or log.duplicate:
                problems.append(f"importação: {len(log.conflicting)} conflito(s), {len(log.duplicate)} duplicata(s)")
            media = set(os.listdir(col.media.dir()))
            guids = set(col.db.list("select guid from notes"))
            by_type: dict[str, int] = {}
            io_notes = io_shapes = 0
            for nid in col.find_notes(""):
                note = col.get_note(nid)
                nt = note.note_type()
                by_type[nt["name"]] = by_type.get(nt["name"], 0) + 1
                cards = note.cards()
                label = f"nota {note.guid} ({nt['name']})"
                if not cards:
                    problems.append(f"{label}: nenhum cartão gerado")
                for name in IMG_SRC.findall(" ".join(note.fields)):
                    if name not in media:
                        problems.append(f"{label}: mídia ausente no pacote: {name}")
                if nt.get("originalStockKind") == IO_STOCK_KIND:
                    io_notes += 1
                    resp = col.get_image_occlusion_note(nid)
                    if resp.WhichOneof("value") != "note":
                        problems.append(f"{label}: o Anki não reconhece a nota como Image Occlusion: {resp.error}")
                    else:
                        occ = resp.note.occlusions
                        shapes = sum(len(o.shapes) for o in occ)
                        io_shapes += shapes
                        if not shapes:
                            problems.append(f"{label}: nenhuma máscara reconhecida")
                        if len(occ) != len(cards):
                            problems.append(f"{label}: {len(occ)} oclusão(ões) para {len(cards)} cartão(ões)")
                        if not resp.note.image_data:
                            problems.append(f"{label}: imagem da oclusão não carregada")
                elif nt["type"] == CLOZE_TYPE:
                    ords = set(CLOZE_ORD.findall(note.fields[0]))
                    if not ords or len(ords) != len(cards):
                        problems.append(f"{label}: {len(ords)} lacuna(s) para {len(cards)} cartão(ões)")
                for card in cards:
                    q, a = card.question(), card.answer()
                    if not re.sub(r"<style.*?</style>|<[^>]+>|\s", "", q, flags=re.S):
                        problems.append(f"{label}: frente vazia")
                    if "{{" in re.sub(r"<script.*?</script>", "", q + a, flags=re.S):
                        problems.append(f"{label}: campo de template não resolvido")
            empty = col.get_empty_cards().notes
            if empty:
                problems.append(f"{len(empty)} nota(s) com cartões vazios")
            scheduled = col.db.scalar(
                "select count() from cards where queue!=0 or type!=0 or ivl!=0 or reps!=0 or lapses!=0"
            )
            revlog = col.db.scalar("select count() from revlog")
            if scheduled or revlog:
                problems.append(f"agendamento presente: {scheduled} cartão(ões) agendado(s), {revlog} revisão(ões)")
            if expect_guids is not None and guids != expect_guids:
                problems.append(
                    f"GUIDs divergentes: faltam {sorted(expect_guids - guids)[:5]}, sobram {sorted(guids - expect_guids)[:5]}"
                )
            report.update(
                notes=col.note_count(),
                cards=col.card_count(),
                notes_by_type=by_type,
                image_occlusion_notes=io_notes,
                image_occlusion_shapes=io_shapes,
                decks=sorted(d.name for d in col.decks.all_names_and_ids() if d.name != "Default"),
                tags=sorted(col.tags.all()),
                media_files=len(media),
                scheduled_cards=scheduled,
                revlog_entries=revlog,
                guids=sorted(guids),
            )
        finally:
            col.close()
    report["ok"] = not problems
    return report


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    result = verify(sys.argv[1])
    result.pop("guids", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
