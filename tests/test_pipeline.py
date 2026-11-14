"""Testes técnicos do fluxo extração → travas → exportação → reimportação.

Todo o conteúdo aqui é sintético e não médico. Estes testes verificam a mecânica dos
pacotes .apkg; não dizem nada sobre a qualidade pedagógica dos cartões.

Rodar: python -m pytest -q tests
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from anki.collection import Collection, ImportAnkiPackageRequest
from PIL import Image, ImageDraw

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from export_apkg import ExportError, export  # noqa: E402
from verify_apkg import verify  # noqa: E402

VERSION = "sha256:" + "0" * 64
PAGE_1 = "O parâmetro alfa do ensaio vale 42 unidades.\nA sequência de calibra-\nção tem etapa inicial e etapa final."
PAGE_2 = "Quando o limite beta é superior a 7,5 mV, a rotina é interrompida."


def meta(card_id: str, page: int, excerpt: str, **extra) -> dict:
    return {
        "card_id": card_id, "source_file": "sources/ensaio.pdf", "source_page": page,
        "source_section": "Seção de teste", "evidence_excerpt": excerpt, "topic": "Teste técnico",
        "priority": 80, "validation_status": "approved", "source_version": VERSION, **extra,
    }


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, dict]:
    """Projeto sintético: fonte com duas páginas, duas imagens e um cartão de cada tipo."""
    (tmp_path / "projects/ensaio").mkdir(parents=True)
    (tmp_path / "media/ensaio").mkdir(parents=True)
    (tmp_path / "cards").mkdir()
    (tmp_path / "projects/ensaio/source_text.json").write_text(json.dumps({
        "source_file": "sources/ensaio.pdf", "source_version": VERSION,
        "units": [{"page": 1, "section": None, "text": PAGE_1}, {"page": 2, "section": None, "text": PAGE_2}],
    }), "utf-8")
    for name in ("esquema.png", "figura.png"):
        im = Image.new("RGB", (800, 500), "white")
        draw = ImageDraw.Draw(im)
        draw.rectangle((80, 100, 280, 180), outline="black")
        draw.text((100, 130), name, fill="black")
        im.save(tmp_path / "media/ensaio" / name)
    visual = {"evidence_kind": "visual", "evidence_image": "media/ensaio/esquema.png"}
    deck = {
        "project": "ensaio",
        "deck": "Teste::Técnico::Ação",
        "sources": [{"source_file": "sources/ensaio.pdf", "source_version": VERSION,
                     "source_text": "projects/ensaio/source_text.json"}],
        "cards": [
            {**meta("e-001", 1, "O parâmetro alfa do ensaio vale 42 unidades."), "card_type": "basic",
             "front": "Qual é o valor do parâmetro alfa do ensaio?", "back": "<b>42 unidades</b>",
             "extra": "Observação técnica.", "tags": ["Teste::Sub", "Definição"]},
            {**meta("e-002", 1, "A sequência de calibração tem etapa inicial e etapa final."), "card_type": "cloze",
             "text": "A sequência de calibração tem etapa {{c1::inicial}} e etapa {{c2::final}}.", "tags": ["Teste::Sub"]},
            {**meta("e-003", 2, "Quando o limite beta é superior a 7,5 mV, a rotina é interrompida."),
             "card_type": "clinical_reasoning", "deck": "Teste::Técnico::Decisão",
             "front": "O limite beta está acima de 7,5 mV. O que acontece com a rotina?", "back": "É interrompida.",
             "tags": ["Conduta"]},
            {**meta("e-004", 1, "", **visual), "card_type": "image_occlusion", "image": "media/ensaio/esquema.png",
             "header": "Esquema de teste", "occlusion_mode": "hide_all_guess_one", "tags": ["Imagem"],
             "occlusions": [
                 {"shape": "rect", "x": 80, "y": 100, "w": 200, "h": 80, "label": "A"},
                 {"shape": "ellipse", "x": 480, "y": 300, "w": 200, "h": 80, "label": "B"},
                 {"shape": "polygon", "points": [[80, 400], [160, 400], [120, 480]], "label": "C"},
             ]},
            {**meta("e-005", 1, "", **visual), "card_type": "image_occlusion", "image": "media/ensaio/esquema.png",
             "occlusion_mode": "hide_one_guess_one", "tags": ["Imagem"],
             "occlusions": [{"shape": "rect", "x": 0, "y": 0, "w": 100, "h": 50},
                            {"shape": "rect", "x": 700, "y": 450, "w": 100, "h": 50}]},
            {**meta("e-006", 1, "O parâmetro alfa do ensaio vale 42 unidades."), "card_type": "basic",
             "front": "O que a figura mostra?<br><img src=\"figura.png\">", "back": "O parâmetro alfa.",
             "media": ["media/ensaio/figura.png"]},
            {**meta("e-007", 1, "O parâmetro alfa do ensaio vale 42 unidades."), "card_type": "basic",
             "front": "Cartão ainda em revisão?", "back": "Sim.", "validation_status": "needs_review"},
        ],
    }
    return tmp_path, deck


def run(root: Path, deck: dict, **kwargs) -> dict:
    path = root / "cards/ensaio.json"
    path.write_text(json.dumps(deck, ensure_ascii=False), "utf-8")
    return export(path, root=root, **kwargs)


def open_imported(tmp: Path, apkg: Path) -> Collection:
    col = Collection(str(tmp / f"check-{time.time_ns()}.anki2"))
    col.import_anki_package(ImportAnkiPackageRequest(package_path=str(apkg)))
    return col


@pytest.mark.parametrize("modern", [False, True])
def test_exporta_e_reimporta_todos_os_tipos(project, modern):
    root, deck = project
    manifest = run(root, deck, modern=modern)
    apkg = root / "exports/ensaio.apkg"
    assert apkg.is_file() and (root / "exports/ensaio.manifest.json").is_file()
    assert manifest["notes"] == 6
    assert manifest["cards"] == 1 + 2 + 1 + 3 + 2 + 1
    assert manifest["notes_by_type"] == {"MedElite Basic": 3, "MedElite Cloze": 1, "MedElite Image Occlusion": 2}
    assert manifest["image_occlusion"] == {"notes": 2, "shapes": 5}
    assert manifest["scheduled_cards"] == 0 and manifest["revlog_entries"] == 0
    assert manifest["skipped"] == [{"card_id": "e-007", "validation_status": "needs_review"}]
    assert {"Teste::Técnico::Ação", "Teste::Técnico::Decisão"} <= set(manifest["decks"])
    assert {"Teste::Sub", "Definição", "Conduta", "Imagem"} <= set(manifest["tags"])
    assert len(manifest["traceability"]) == 6 and manifest["evidence_checked_visually"] == ["e-004", "e-005"]

    col = open_imported(root, apkg)
    try:
        basic = col.get_note(col.find_notes('"Front:Qual é o valor*"')[0])
        assert basic["Front"] == "Qual é o valor do parâmetro alfa do ensaio?"
        assert basic["Back"] == "<b>42 unidades</b>" and basic["Extra"] == "Observação técnica."
        assert basic["Source"] == "ensaio.pdf, p. 1 — Seção de teste"
        answer = basic.cards()[0].answer()
        assert "42 unidades" in answer and "Observação técnica." in answer and "ensaio.pdf" in answer
        assert sorted(os.listdir(col.media.dir())) == ["esquema.png", "figura.png"]

        # Image Occlusion nativo: o próprio Anki reconhece o modelo e interpreta as máscaras
        io_all, io_one = (col.get_note(n) for n in sorted(col.find_notes("tag:Imagem")))
        if "oi=1" not in io_all["Occlusion"]:
            io_all, io_one = io_one, io_all
        assert io_all.note_type()["originalStockKind"] == 6
        assert io_all["Occlusion"].count(":oi=1") == 3 and "oi=1" not in io_one["Occlusion"]
        parsed = col.get_image_occlusion_note(io_all.id).note
        shapes = {o.ordinal: (o.shapes[0].shape, {p.name: p.value for p in o.shapes[0].properties}) for o in parsed.occlusions}
        assert shapes[1] == ("rect", {"left": ".1", "top": ".2", "width": ".25", "height": ".16", "oi": "1"})
        assert shapes[2] == ("ellipse", {"left": ".6", "top": ".6", "rx": ".125", "ry": ".08", "oi": "1"})
        assert shapes[3] == ("polygon", {"points": ".1,.8 .2,.8 .15,.96", "oi": "1"})
        assert parsed.occlude_inactive and not col.get_image_occlusion_note(io_one.id).note.occlude_inactive
        assert len(io_all.cards()) == 3 and len(io_one.cards()) == 2
        assert io_all["Comments"].startswith("ensaio.pdf")
        front = io_all.cards()[0].question()
        assert "anki.imageOcclusion.setup()" in front and "esquema.png" in front
    finally:
        col.close()


@pytest.mark.parametrize("modern", [False, True])
def test_reexportar_atualiza_sem_duplicar(project, modern):
    """Simula o Anki do usuário: importa a v1, depois a v2 com um cartão corrigido e um novo."""
    root, deck = project
    run(root, deck, modern=modern)
    user = Collection(str(root / "usuario.anki2"))
    try:
        user.import_anki_package(ImportAnkiPackageRequest(package_path=str(root / "exports/ensaio.apkg")))
        notetypes = len(user.models.all_names_and_ids())
        time.sleep(1.1)  # o Anki só atualiza notas com data de modificação mais recente

        v2 = copy.deepcopy(deck)
        v2["cards"][0]["back"] = "<b>42 unidades</b> (corrigido)"
        v2["cards"].append({**meta("e-008", 2, "a rotina é interrompida"), "card_type": "basic",
                            "front": "Cartão novo da segunda versão?", "back": "Sim."})
        run(root, v2, modern=modern)
        log = user.import_anki_package(ImportAnkiPackageRequest(package_path=str(root / "exports/ensaio.apkg"))).log

        assert len(log.conflicting) == 0
        assert len(log.new) == 1 and len(log.updated) == 1  # só o cartão corrigido é sobrescrito
        assert user.note_count() == 7
        assert len(user.models.all_names_and_ids()) == notetypes
        assert user.get_note(user.find_notes('"Front:Qual é o valor*"')[0])["Back"].endswith("(corrigido)")

        # edição feita dentro do Anki num cartão que não mudou aqui: a v3 não pode apagá-la
        time.sleep(1.1)
        mine = user.get_note(user.find_notes('"Front:O limite beta*"')[0])
        mine["Back"] = "Minha edição no Anki."
        user.update_note(mine)
        css = user.models.by_name("MedElite Basic")
        css["css"] += "\n.card { font-size: 24px; }"
        user.models.update_dict(css)
        time.sleep(1.1)
        v3 = copy.deepcopy(v2)
        v3["cards"][1]["extra"] = "Complemento novo."
        run(root, v3, modern=modern)
        log = user.import_anki_package(ImportAnkiPackageRequest(package_path=str(root / "exports/ensaio.apkg"))).log
        assert len(log.conflicting) == 0 and len(log.new) == 0 and len(log.updated) == 1
        assert user.get_note(mine.id)["Back"] == "Minha edição no Anki."
        assert "font-size: 24px" in user.models.by_name("MedElite Basic")["css"]
        assert user.note_count() == 7 and len(user.models.all_names_and_ids()) == notetypes
    finally:
        user.close()


@pytest.mark.parametrize("change, message", [
    (lambda c: c[0].update(evidence_excerpt="O parâmetro gama do ensaio vale 42 unidades."), "evidence_excerpt não encontrado"),
    (lambda c: c[0].update(source_page=2), "evidence_excerpt não encontrado"),
    (lambda c: c[0].update(back="<b>43 unidades</b>"), "sem respaldo na página da fonte: 43"),
    (lambda c: c[2].update(front="O limite beta está acima de 7.5 mV e abaixo de 9 mV?"), "sem respaldo na página da fonte: 9"),
    (lambda c: c[0].pop("topic"), "metadado ausente: topic"),
    (lambda c: c[0].update(evidence_excerpt=""), "evidence_excerpt ausente"),
    (lambda c: c[0].update(source_file="sources/outro.pdf"), "não está entre as fontes autorizadas"),
    (lambda c: c[0].update(source_version="sha256:" + "1" * 64), "source_version não corresponde"),
    (lambda c: c[1].update(text="Texto sem lacuna."), "cloze sem lacunas válidas"),
    (lambda c: c[3]["occlusions"][0].update(x=700), "máscara inválida"),
    (lambda c: c[3].update(evidence_image="media/ensaio/ausente.png"), "evidência visual sem evidence_image"),
    (lambda c: c[5].update(media=[]), "imagem referenciada sem arquivo"),
    (lambda c: c[5].update(front=c[0]["front"]), "frente idêntica"),
    (lambda c: c[5].update(card_id="e-001"), "card_id repetido"),
])
def test_travas_cancelam_a_exportacao(project, change, message):
    root, deck = project
    change(deck["cards"])
    with pytest.raises(ExportError, match=message):
        run(root, deck)
    assert not (root / "exports/ensaio.apkg").exists()


def test_fonte_sem_texto_exige_evidencia_visual(project):
    root, deck = project
    deck["sources"][0]["source_text"] = None
    with pytest.raises(ExportError, match="fonte sem texto extraído"):
        run(root, deck)


def test_verificador_detecta_pacote_adulterado(project):
    root, deck = project
    run(root, deck)
    report = verify(root / "exports/ensaio.apkg", expect_guids={"mea-inexistente"})
    assert not report["ok"] and "GUIDs divergentes" in report["problems"][0]


def extract(root: Path, *args: str) -> str:
    cmd = [sys.executable, str(SCRIPTS / "extract_source.py"), *args, "--project", "ext", "--root", str(root)]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def test_extrai_pdf_com_paginas_imagens_e_recorte(tmp_path):
    import pymupdf

    figure = tmp_path / "fig.png"
    Image.new("RGB", (300, 200), "navy").save(figure)
    pdf = tmp_path / "fonte.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Primeira pagina do documento de teste: valor 42 unidades.")
    page.insert_image(pymupdf.Rect(72, 100, 372, 300), filename=str(figure))
    doc.new_page().insert_text((72, 72), "Segunda pagina do documento de teste: limite 7,5 mV.")
    doc.new_page()
    doc.save(pdf)
    doc.close()

    out = json.loads(extract(tmp_path, str(pdf)))
    dump = json.loads((tmp_path / out["source_text"]).read_text("utf-8"))
    assert out["units"] == 3 and out["images"] == 1 and out["low_text_units"] == [3]
    assert dump["source_version"].startswith("sha256:") and len(dump["source_version"]) == 71
    assert "42 unidades" in dump["units"][0]["text"] and "7,5 mV" in dump["units"][1]["text"]
    image = dump["units"][0]["images"][0]
    assert (image["width"], image["height"]) == (300, 200) and (tmp_path / image["file"]).is_file()
    registry = json.loads((tmp_path / "sources/registry.json").read_text("utf-8"))
    assert registry[0]["source_version"] == dump["source_version"] and registry[0]["project"] == "ext"

    extract(tmp_path, str(pdf), "--render-pages", "--pages", "1-2")
    assert sorted(p.name for p in (tmp_path / "projects/ext/pages").iterdir()) == ["p0001.png", "p0002.png"]
    clip = extract(tmp_path, str(pdf), "--clip", "1,72,100,372,300", "--dpi", "144").strip()
    assert Image.open(tmp_path / clip).size == (600, 400)


def test_extrai_docx_pptx_e_markdown(tmp_path):
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches

    figure = tmp_path / "fig.png"
    Image.new("RGB", (120, 90), "white").save(figure)

    word = Document()
    word.add_heading("Seção A", level=1)
    word.add_paragraph("Texto da seção A com acentuação.")
    word.add_heading("Seção B", level=1)
    word.add_paragraph("Texto da seção B.")
    word.add_picture(str(figure))
    word.save(tmp_path / "fonte.docx")
    dump = json.loads((tmp_path / json.loads(extract(tmp_path, str(tmp_path / "fonte.docx")))["source_text"]).read_text("utf-8"))
    assert [u["section"] for u in dump["units"]] == ["Seção A", "Seção B"]
    assert "acentuação" in dump["units"][0]["text"] and len(dump["units"][0]["images"]) == 1

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Slide de teste"
    slide.shapes.add_picture(str(figure), Inches(1), Inches(2))
    deck.save(tmp_path / "fonte.pptx")
    dump = json.loads((tmp_path / json.loads(extract(tmp_path, str(tmp_path / "fonte.pptx")))["source_text"]).read_text("utf-8"))
    assert dump["units"][0]["page"] == 1 and "Slide de teste" in dump["units"][0]["text"]
    assert len(dump["units"][0]["images"]) == 1

    (tmp_path / "fonte.md").write_text("# Um\ntexto um\n\n## Dois\ntexto dois\n", "utf-8")
    dump = json.loads((tmp_path / json.loads(extract(tmp_path, str(tmp_path / "fonte.md")))["source_text"]).read_text("utf-8"))
    assert [u["section"] for u in dump["units"]] == ["Um", "Dois"]


def test_mascaras_agrupadas_viram_um_cartao(project):
    root, deck = project
    occlusions = deck["cards"][3]["occlusions"]
    occlusions[0]["group"] = occlusions[1]["group"] = 1
    occlusions[2]["group"] = 2
    run(root, deck)
    col = open_imported(root, root / "exports/ensaio.apkg")
    try:
        note = next(col.get_note(n) for n in col.find_notes("tag:Imagem") if ":oi=1" in col.get_note(n)["Occlusion"])
        parsed = {o.ordinal: len(o.shapes) for o in col.get_image_occlusion_note(note.id).note.occlusions}
        assert parsed == {1: 2, 2: 1} and len(note.cards()) == 2
    finally:
        col.close()
