# VALIDAÇÃO CIENTÍFICA

## Fontes

1. Material fornecido pelo usuário = **fonte primária** do run. Não complementar silenciosamente com conhecimento externo ou do treinamento do modelo.
2. Verificação ou expansão externa só com autorização do usuário para o run (`config/settings.json` → `external_verification`). Quando houver, distinguir no cartão o que veio do material (`evidence`) e o que foi confirmado fora (`external_refs`).
3. Hierarquia, quando efetivamente acessíveis: diretrizes/consensos adequados ao contexto → livros-texto de referência (Harrison, Goldman-Cecil, Robbins, Guyton & Hall, Costanzo, Berne & Levy, Goodman & Gilman, Katzung, Sabiston, Schwartz, ATLS, sociedades brasileiras e internacionais) → revisões sistemáticas e estudos → materiais didáticos → outras fontes avaliadas criticamente.
4. A lista é preferência bibliográfica. **Nunca** citar obra não consultada nesta sessão. Referência fictícia é eliminatória.

## Rastreabilidade (cada cartão, em `candidates.jsonl`)

- `source_ref`: `{file, page | section}`.
- `evidence`: trecho **literal** do texto extraído (string ou lista). O validador confere a presença na página citada.
- `external_refs`: `[{ref: "obra, edição/ano, página ou URL", excerpt: "trecho literal", accessed: "AAAA-MM-DD"}]`.
- `verification_status`: `fonte_primaria` | `verificado_externo` | `pendente`.
- Números da frente e do verso precisam constar dos trechos de sustentação (bloqueio automático).
- Termos do verso ausentes da fonte geram aviso: sinal de conhecimento externo infiltrado. Ler e resolver.

## Informação atualizável (R08)

Diretrizes, algoritmos, doses, classificações, estadiamentos, rastreamento, critérios: registrar o ano/versão da fonte em `note` quando identificável e qualificar a frente quando a resposta depender da versão ("Segundo [diretriz, ano]..."). Sem possibilidade de confirmação → `gate: revisao_cientifica`, `verification_status: pendente`, sem alegar atualização.

## Divergências

1. Identificar. 2. Verificar data, contexto e população de cada fonte. 3. Consultar referência atualizada, se autorizado e disponível. 4. Registrar a interpretação e a origem em `note`. 5. Persistindo, `revisao_cientifica`. Nunca esconder contradição.

## Imagens

Só quando indispensáveis ou realmente úteis (ECG, imagem radiológica, histopatologia, dermatologia, anatomia). Fiel ao material, legível, sem achado visual inventado. Extração visual não confiável → sinalizar e não interpretar.

## Limites do validador

As travas são lexicais: provam que o cartão não cita número ou trecho que a fonte não tem. Não provam interpretação correta, resposta única ou ausência de duplicata semântica. Isso é a auditoria semântica feita pelo modelo, cartão a cartão, antes de marcar `gate`.
