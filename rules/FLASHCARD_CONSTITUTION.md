# CONSTITUIÇÃO PEDAGÓGICA — EASYCARDS BASIC v1.00

Núcleo normativo do projeto. Imutável sem autorização explícita do usuário.
Especificação autossuficiente: não existe documentação externa a consultar sobre "EASYCARDS BASIC".

Ordem obrigatória de prioridades (nenhuma otimização compromete uma superior):

**Segurança e precisão científica → relevância médica → atomicidade → clareza → recuperação ativa → compatibilidade técnica → eficiência computacional.**

> Um flashcard excelente não é aquele que contém mais conhecimento. É aquele que permite recuperar, com precisão e mínimo esforço cognitivo desnecessário, uma informação importante, independente e clinicamente correta.

O produto é sempre **BASIC** (Frente, Verso, Tag). Sem Cloze, sem questão discursiva disfarçada. O agendamento é do Anki (inclusive FSRS); o projeto não implementa repetição espaçada.

## Regras

**R01 — Atomicidade absoluta.** Um cartão = uma pergunta = uma resposta-alvo inequívoca. Proibido no mesmo cartão: duas perguntas independentes; dois critérios diagnósticos distintos; múltiplas condutas independentes; listas de etiologias; conjuntos extensos de manifestações; definição + mecanismo + tratamento. Um trecho com cinco fatos é avaliado fato a fato: até cinco cartões, só para os que importam.

**R02 — Informação mínima.** Frente com o contexto indispensável para a resposta ser correta e única; verso só com o que responde. Frente ≤ 40 palavras (ideal 8–25). Verso idealmente 1–8 palavras; mais longo só quando a precisão exigir (`long_answer_reason`). Sem parágrafos, explicações ou listas. Informação mínima serve à clareza, não produz frases criptográficas.

**R03 — Recuperação ativa.** Exigir lembrança de conceito, diagnóstico, achado, valor de corte, mecanismo, agente, conduta, contraindicação ou associação. A resposta não pode ser dedutível pela redação.

**R04 — Resposta única.** Teste: um estudante competente poderia dar duas respostas diferentes e corretas? Se sim: acrescentar qualificador (população, cenário, faixa etária, gravidade, localidade, versão de diretriz), reformular ou eliminar. "Principal causa", "agente mais comum" e "tratamento de escolha" sempre com contexto.

**R05 — Independência.** Compreensível isoladamente, em ordem aleatória. Proibido depender de outro cartão, de numeração de lista, de "e o segundo?" ou de contexto implícito.

**R06 — Relevância.** Elegível só com valor para: provas médicas; reconhecimento diagnóstico; decisão clínica; fisiopatologia; interpretação de exames; prevenção de erro clínico importante; discussão acadêmica. Curadoria, não conversão integral do material.

**R07 — Precisão terminológica.** Nomenclatura técnica padronizada; preservar anatomia, nomes de doenças, unidades, critérios, faixas de referência, doses contextualizadas, nomes farmacológicos e siglas reconhecidas. Sem sinônimos imprecisos.

**R08 — Sem ambiguidade temporal.** Diretrizes, algoritmos de emergência, recomendações farmacológicas, classificações, estadiamentos, rastreamentos e critérios atualizáveis: nunca como verdade universal. Sem confirmação possível, registrar a limitação e mandar para `revisao_cientifica`; nunca alegar atualização inexistente. Ver `MEDICAL_VALIDATION.md`.

**R09 — Sem redundância inútil.** Dois cartões sobre o mesmo conceito só com formas de recuperação genuinamente complementares e justificáveis. Nada de variações para inflar a contagem.

**R10 — Estilo de residência.** Precisão lexical e objetividade de prova, mas o cartão isola o conhecimento; não é questão de prova.

## Modelos de referência (padrões gerais, nunca fonte de conteúdo; não copiar cartões nem marcas)

- **A — Bradiarritmias:** perguntas diretas; definições e padrões clássicos; reconhecimento de ECG; diferenciação; valores objetivos; condutas. Adaptar: contexto clínico suficiente; ECG só com a imagem disponível e autorizada.
- **B — Pneumonia e derrame pleural:** terminologia de prova; critérios, pontos de corte, classificações, semiologia, condutas. Adaptar: itens múltiplos viram cartões independentes; V/F vira pergunta objetiva quando melhora a recuperação; lacuna só com resposta única e sem pista artificial.
- **C — Fraqueza muscular (Anki):** localização ↔ função; síndromes; correlação semiológica; imagem como reforço ou estímulo. Adaptar: sem respostas longas, perguntas compostas ou extras no verso; imagem só se indispensável, sem sair do BASIC.
- **D — Estudo com Anki:** active recall; repetição espaçada pelo Anki; organização hierárquica; modularidade; importação confiável; estudo seletivo. Não copiar parâmetros de FSRS de terceiros.

Síntese: direção objetiva + informação mínima + alta relevância + precisão clínica + organização modular + exportação confiável.

## Matriz de priorização (campo `priority`)

- **A — Essencial (gerar prioritariamente):** definições fundamentais; critérios diagnósticos; achados típicos e discriminativos; valores de corte; agentes clássicos; diferenciais fundamentais; mecanismos centrais; condutas de primeira linha; contraindicações importantes; sinais de gravidade; complicações frequentes ou perigosas; erros clínicos relevantes.
- **B — Complementar de alto valor:** particularidades semiológicas; relações anatomoclínicas; exceções importantes; apresentações atípicas com impacto diagnóstico; dados para diferenciar doenças semelhantes.
- **C — Baixo retorno (excluir por padrão):** curiosidades históricas; raridades; periferia; listas pouco discriminativas; estatística sem utilidade; memorização desproporcional. Exceção: raro que evita erro grave ou com relevância comprovada para a prova escolhida.

Frequência em prova só se houver evidência; caso contrário a prioridade é julgamento editorial e é dita como tal.

## Formatos de pergunta preferenciais

| Tipo | Molde |
|---|---|
| Definição | Qual é a definição de [conceito]? |
| Reconhecimento | Qual diagnóstico é caracterizado por [achado discriminativo]? |
| Fisiopatologia | Qual mecanismo explica [fenômeno]? |
| Semiologia | Como se altera [sinal] na presença de [condição]? |
| Critério | Qual é o valor de corte de [parâmetro] para [diagnóstico]? |
| Etiologia | Qual é o agente mais comum de [doença], em [população/cenário]? |
| Conduta | Em [situação definida], qual é a conduta inicial indicada? |
| Farmacologia | Qual receptor é antagonizado por [fármaco]? |
| Diferenciação | Qual achado distingue [A] de [B] em [contexto]? |
| Imagem | Qual alteração está presente no ECG apresentado? (só com imagem clara e interpretação validada) |

Transformações: "Quais são todos os critérios do CURB-65?" → um cartão por componente/limite. "Explique o BAV de 1º grau, fisiopatologia, achados e tratamento" → um cartão por unidade (ex.: "Qual alteração do intervalo PR caracteriza o BAV de primeiro grau?"). Exemplos ensinam forma, não conteúdo.

## Quality Gate (campo `gate`, decidido na auditoria semântica)

Critérios: atomicidade · unicidade · clareza isolada · precisão científica sustentada · relevância · concisão · recuperação ativa · contextualização sem omissão que altere a resposta · não redundância · compatível com BASIC.

- `aprovado` — satisfaz todos.
- `reescrever` — problema corrigível de redação, atomicidade ou contexto.
- `revisao_cientifica` — depende de esclarecimento de fonte, recomendação ou evidência.
- `descartado` — ambíguo, irrelevante, insustentável ou impossível em BASIC.

**Eliminatórias:** informação inventada; resposta incorreta; sem resposta-alvo única; pergunta composta; verso com fatos independentes; contradição não resolvida; referência fictícia; repetição semântica desnecessária; tratamento apresentado como universal quando depende de variáveis omitidas.

Boa pontuação de estilo nunca aprova cartão cientificamente inadequado. Aprovação humana não substitui verificação científica.

## Tags

`ÁREA::SUBÁREA::ASSUNTO`, exatamente três níveis, ASCII sem acento, CamelCase sem espaços (ex.: `Cardiologia::Arritmias::BloqueioAV`, `Pneumologia::Pleura::DerramePleural`). Antes de criar uma tag nova, reutilizar a existente equivalente (o validador acusa grafias conflitantes).
