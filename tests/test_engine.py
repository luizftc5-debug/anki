"""Testes técnicos com conteúdo sintético (não médico): extração, auditoria, aprovação e exportação."""
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import engine  # noqa: E402

SOURCE = """# Capítulo sintético

O marcador Zeta é definido por valor acima de 120 unidades.
A estrutura Alfa conecta o compartimento Beta ao compartimento Gama.

# Conduta sintética

Na situação Delta estável, a conduta inicial é observar e repetir a medida.
"""


def card(i, **kw):
    base = {"id": f"{i:03d}", "front": "Qual valor define o marcador Zeta?", "back": "Acima de 120 unidades",
            "tag": "Teste::Sintetico::Marcadores", "priority": "A", "gate": "aprovado",
            "verification_status": "fonte_primaria", "source_ref": {"file": "sources/fonte.md", "section": "Capítulo sintético"},
            "evidence": "O marcador Zeta é definido por valor acima de 120 unidades.", "external_refs": [],
            "image": None, "image_side": None, "note": ""}
    base.update(kw)
    return base


@pytest.fixture
def env(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "fonte.md").write_text(SOURCE, "utf-8")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "extract.py"), str(tmp_path / "sources" / "fonte.md"),
                          "--run", "teste", "--root", str(tmp_path)], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["units"] == 2
    run = tmp_path / "runs" / "teste"
    Image.new("RGB", (120, 90), "white").save(run / "media" / "fig.png")
    return tmp_path, run


def write(run, cards):
    (run / "candidates.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cards), "utf-8")


def good_set():
    return [
        card(1),
        card(2, front="Qual estrutura conecta o compartimento Beta ao compartimento Gama?", back="Estrutura <b>Alfa</b>",
             tag="Teste::Sintetico::Estruturas", evidence="A estrutura Alfa conecta o compartimento Beta ao compartimento Gama.",
             image="runs/teste/media/fig.png", image_side="back"),
        card(3, front="Na situação Delta estável, qual é a conduta inicial?", back="Observar e repetir a medida",
             tag="Teste::Sintetico::Conduta", source_ref={"file": "sources/fonte.md", "section": "Conduta sintética"},
             evidence="Na situação Delta estável, a conduta inicial é observar e repetir a medida."),
    ]


def test_extract_uses_cache(env):
    root, _ = env
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "extract.py"), str(root / "sources" / "fonte.md"),
                          "--run", "teste", "--root", str(root)], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["cache"] is True
    assert "teste" in next(iter(json.loads((root / "data" / "manifest.json").read_text()).values()))["runs"]


def test_good_cards_pass(env):
    root, run = env
    write(run, good_set())
    report = engine.audit(run, root)
    assert all(not v["errors"] for v in report["cards"].values()), report


@pytest.mark.parametrize("override, expected", [
    ({"front": " ".join(["palavra"] * 41) + "?"}, "frente com 41 palavras"),
    ({"front": "Qual valor define Zeta? Qual a unidade?"}, "mais de uma pergunta"),
    ({"tag": "Teste::Sintético::Marcadores"}, "tag inválida"),
    ({"tag": "Teste::Sintetico"}, "tag inválida"),
    ({"back": "Acima de 150 unidades"}, "número sem sustentação"),
    ({"evidence": "O marcador Zeta é definido por valor acima de 999 unidades."}, "não encontrado literalmente"),
    ({"evidence": None}, "sem evidência"),
    ({"front": "Explique o marcador Zeta"}, "padrão proibido"),
    ({"front": "Qual valor define o {{c1::marcador}}?"}, "padrão proibido"),
    ({"back": " ".join(["x"] * 21)}, "sem long_answer_reason"),
    ({"verification_status": "pendente"}, "pendente não pode"),
    ({"image": "runs/teste/media/nao_existe.png", "image_side": "front"}, "imagem não encontrada"),
    ({"external_refs": [{"ref": "Obra, 2024, p. 1", "excerpt": "120"}]}, "não pode ter verification_status fonte_primaria"),
])
def test_eliminatory_errors(env, override, expected):
    root, run = env
    write(run, [card(1, **override)])
    errors = engine.audit(run, root)["cards"]["001"]["errors"]
    assert any(expected in e for e in errors), errors


def test_duplicates_and_tag_conflicts(env):
    root, run = env
    write(run, [card(1), card(2), card(3, front="Qual valor define o marcador Zeta, em unidades?", tag="teste::sintetico::marcadores")])
    report = engine.audit(run, root)
    assert any("frente idêntica" in e for e in report["cards"]["002"]["errors"])
    assert report["tag_conflicts"]


def test_warnings_for_long_back_and_external_terms(env):
    root, run = env
    write(run, [card(1, back="Acima de 120 unidades conforme o critério hepatoesplênico do marcador")])
    warns = engine.audit(run, root)["cards"]["001"]["warnings"]
    assert any("verso com" in w for w in warns)
    assert any("termo do verso ausente" in w for w in warns)


def test_export_blocked_without_approval(env):
    root, run = env
    write(run, good_set())
    with pytest.raises(SystemExit, match="nenhum cartão aprovado"):
        engine.export(run, root)


def test_approval_refuses_bad_or_unready_cards(env):
    root, run = env
    cards = good_set()
    cards[2]["gate"] = "revisao_cientifica"
    cards[2]["verification_status"] = "pendente"
    write(run, cards)
    with pytest.raises(SystemExit, match="recusada"):
        engine.approve(run, ["003"], root)
    result = engine.approve(run, None, root)
    assert result == {"aprovados": 2, "nao_aprovaveis_ainda": ["003"]}


def test_change_after_approval_blocks_export(env):
    root, run = env
    cards = good_set()
    write(run, cards)
    engine.approve(run, None, root)
    cards[0]["back"] = "Acima de 120 unidades (sintético)"
    write(run, cards)
    assert engine.approval_state(run, cards)["001"] == "alterado_apos_aprovacao"
    with pytest.raises(SystemExit, match="alterados após a aprovação"):
        engine.export(run, root)
    cards[0]["note"] = "metadado não exige reaprovação"
    cards[0]["back"] = "Acima de 120 unidades"
    write(run, cards)
    assert engine.approval_state(run, cards)["001"] == "aprovado"


def test_full_export_csv_and_apkg(env):
    root, run = env
    cards = good_set() + [card(4, front="Pergunta descartada?", gate="descartado"),
                          card(5, front="Qual é o marcador definido por valor acima de 120 unidades?", back="Zeta",
                               tag="Teste::Sintetico::Marcadores")]
    write(run, cards)
    engine.approve(run, ["001", "002", "003"], root)
    (run / "run.json").write_text(json.dumps({"deck": "Medicina::Teste::Sintético"}), "utf-8")
    result = engine.export(run, root)
    assert result["exportados"] == 3
    assert result["nao_exportados_pendentes"] == ["005"]
    with (root / result["csv"]).open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["Frente", "Verso", "Tag"] and len(rows) == 4 and all(len(r) == 3 for r in rows)
    assert 'src="teste_fig.png"' in rows[2][1]
    assert "Sintético" not in "".join(rows[1]) and rows[1][2] == "Teste::Sintetico::Marcadores"
    assert result["verificacao"]["reimportacao_sem_duplicar"] == "ok"
    # reexportação após editar e reaprovar mantém GUIDs (sem duplicar)
    cards[0]["back"] = "Valor acima de 120 unidades"
    write(run, cards)
    engine.approve(run, ["001"], root)
    assert engine.export(run, root)["exportados"] == 3


def test_preview_pages(env):
    root, run = env
    write(run, good_set())
    out = engine.preview(run, 2, 3, root)
    assert "| 2 | 002 |" in out and "| 3 | 003 |" in out and "| 1 | 001 |" not in out
    text = (run / "review.md").read_text("utf-8")
    assert "[img runs/teste/media/fig.png]" not in text  # imagem só entra na exportação
    assert "## Resumo" in text and "candidatos: 3" in text


def test_feedback_general_rule_is_persisted(env):
    root, _ = env
    (root / "data").mkdir(exist_ok=True)
    args = type("A", (), dict(run="teste", card="001", problem="frente longa", correction="encurtar",
                               solution="removido contexto redundante", kind="concisao", scope="geral",
                               rule="Frentes de definição sem preâmbulo"))
    engine.feedback(args, root)
    cfg = json.loads((root / "config" / "settings.json").read_text())
    assert cfg["learned_rules"][-1]["rule"] == "Frentes de definição sem preâmbulo"
    assert (root / "data" / "settings_history.jsonl").read_text().strip()
    args.scope, args.rule = "geral", None
    with pytest.raises(SystemExit):
        engine.feedback(args, root)
