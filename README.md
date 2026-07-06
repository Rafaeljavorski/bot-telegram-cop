# Bot COP - sem aviso repetido em tópico finalizado

Correção:
- Se alguém clicar novamente no botão Finalizar de um tópico já finalizado, o bot ignora silenciosamente.
- Não envia mais mensagens repetidas como "COP-0006 já foi finalizado".
- Mantém as demais correções: sem mensagens de fixação, travas de assumir/finalizar e PostgreSQL.

Substitua no GitHub:
- bot_cop_telegram.py
- requirements.txt
