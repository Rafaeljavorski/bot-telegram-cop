# Bot COP - sem mensagens de fixação

Correções:
- Remove o pin automático do painel e do cabeçalho do tópico.
- Evita mensagens automáticas "COP CIP Telecom fixou..." no grupo.
- Ignora mensagens de serviço do Telegram, para não responder "Use /start" no grupo.
- Mantém travas de Assumir/Finalizar e PostgreSQL.

Substitua no GitHub:
- bot_cop_telegram.py
- requirements.txt
