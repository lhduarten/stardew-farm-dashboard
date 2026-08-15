Joguei Stardew Valley no fim de semana e resolvi transformar meu save num dashboard.

O arquivo do jogo é um XML de ~4MB. Escrevi um script em Python pra ler esse XML direto, calcular métricas reais da minha fazenda (finanças, plantações, produção) e montar um dashboard.

A parte mais legal foi perceber que nem todo número "dá pra confiar" — o próprio jogo às vezes agrupa dados de um jeito que engana (ex: junta vinhos de frutas diferentes num único contador). Precisei separar o que era dado real do que era suposição, e descartar métrica que não fechava.

Resultado: descobri que processar minha matéria-prima em vez de vender crua rende +222% de valor no meu estoque atual. 🌾

Dashboard: [link]
Código: [link]

#Python #ETL #StardewValley
