# Painel Operacional — Stardew Valley

Pipeline de ETL que lê o save do Stardew Valley (XML) e gera um dashboard
operacional local, com métricas reais de fazenda, finanças, plantações e
exploração — sem inventar dados que o save não sustenta.

**[Ver o dashboard ao vivo →](https://lhduarten.github.io/stardew-farm-dashboard/)**

## Pipeline

```
save do jogo (XML)  →  parse_save.py (ETL)  →  data.json  →  index.html
```

- [`parse_save.py`](parse_save.py) — lê o arquivo de save (`xml.etree.ElementTree`),
  extrai e calcula todas as métricas, e escreve `data.json`.
- [`data.json`](data.json) — saída do ETL: um snapshot estruturado do save no
  momento em que o script foi executado (não é atualizado automaticamente).
- [`index.html`](index.html) — dashboard estático (Tailwind CSS + Chart.js via
  CDN), lê `data.json` via `fetch` e renderiza tudo no navegador.

## Rodar localmente

```bash
python parse_save.py   # gera/atualiza data.json a partir do seu save
python -m http.server 8000
# abrir http://localhost:8000
```

## Princípios do projeto

- **Só métrica que o save sustenta.** Nada de "receita histórica por produto" —
  o próprio jogo agrupa vinho/geleia/conserva/suco sob o mesmo id no contador
  de itens enviados, então não dá pra saber qual fruta específica foi vendida.
  Preferimos remover a métrica a mostrar um número inventado.
- **Localizações fantasmas excluídas.** O save reserva 8 slots de "Cellar"
  para multiplayer, pré-populados pelo próprio jogo mesmo sem cabana
  construída. `parse_save.py` filtra essas localizações antes de contar
  qualquer coisa.
- **Metas explícitas, não escondidas.** A aba "Meta × Realizado" mostra o
  número real ao lado da meta e o quanto falta — sem texto explicativo.

## Estrutura

```
dashboard/
├── parse_save.py              # ETL
├── data.json                  # saída do ETL (snapshot)
├── index.html                 # dashboard (precisa de servidor local/estático)
├── painel_fazenda_duarte.html # versão standalone (dados embutidos, sem servidor)
└── stardew_logo.png
```
