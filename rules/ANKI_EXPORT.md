# EXPORTAÇÃO — CSV E .APKG

## Schema de `runs/<run>/candidates.jsonl` (um objeto por linha)

```json
{"id": "001", "front": "...", "back": "...", "tag": "Area::Subarea::Assunto",
 "priority": "A", "gate": "aprovado", "verification_status": "fonte_primaria",
 "source_ref": {"file": "sources/x.pdf", "page": 3}, "evidence": "trecho literal",
 "external_refs": [], "image": null, "image_side": null,
 "long_answer_reason": null, "note": ""}
```

- `id`: permanente (define o GUID da nota no Anki). Corrigir um cartão mantém o id; nunca reutilizar id de cartão removido — usar `gate: descartado`.
- Conteúdo que exige reaprovação: `front`, `back`, `tag`, `image`, `image_side` (hash em `approval.json`). Metadados podem mudar sem reaprovação.
- HTML permitido com parcimônia (`<b>`, `<sub>`, `<sup>`). Sem listas no verso.
- `image`: caminho relativo à raiz (ex.: `runs/<run>/media/p0004_img01.png`); `image_side`: `front` ou `back`.
- `runs/<run>/run.json` opcional: `{"deck": "Medicina::Cardiologia::Bradiarritmias"}` (padrão `Medicina::<run>`).

## Aprovação (obrigatória)

`engine.py approve` grava o hash do conteúdo de cada cartão. Cartão alterado depois volta a exigir aprovação; exportação com cartão alterado é bloqueada. Só são exportados cartões aprovados pelo usuário, com gate `aprovado` e sem erro de auditoria. `approve --all` aprova os de gate `aprovado`; reescrita e revisão científica ficam de fora. Ausência de objeção não é aprovação.

## CSV — `exports/csv/<run>.csv`

UTF-8, módulo `csv`, cabeçalho `Frente,Verso,Tag`, exatamente três colunas, ordem do arquivo de candidatos. Lido de volta e comparado linha a linha. Imagem aparece como `<img src="<run>_<arquivo>">`; a mídia só viaja no `.apkg`.

## .apkg — `exports/apkg/<run>.apkg`

- `genanki`; modelo `EasyCards Basic` (campos `Front`, `Back`), ID fixo em `config/settings.json`; baralho com ID derivado do nome; GUID = `guid_for(run, id)`.
- Tag do CSV → tag da nota. Nunca visível no cartão.
- Mídia com nome único `<run>_<arquivo>`, sem caminho absoluto.

## Verificação automática (cancela a entrega se falhar)

1. Arquivo existe e não está vazio. 2. ZIP íntegro. 3. `collection.anki2` e `media` presentes. 4. Mídia do pacote = mídia esperada. 5. Importação real numa coleção descartável com o backend oficial do Anki (`pip anki`): número de notas e cartões, campos, tags, baralho, mídia. 6. Segunda importação do mesmo pacote não duplica notas.

**Limite declarado:** importação testada com o backend oficial do Anki; não testada na interface do Anki Desktop, AnkiDroid ou AnkiMobile. Reimportar uma versão corrigida atualiza as notas pelo GUID; o histórico de revisões das notas existentes é mantido pelo Anki, mas isso não foi testado em coleção com revisões.
