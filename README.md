# COP Telegram + Dashboard Web

## Arquivos
- `bot_cop_telegram.py`: bot Telegram com fila, protocolo, conversa técnico ↔ atendente e banco SQLite.
- `dashboard_app.py`: dashboard web Flask.
- `templates/dashboard.html`: tela do dashboard.
- `requirements.txt`: dependências.

## Variáveis no Railway
- `TELEGRAM_BOT_TOKEN`
- `ATENDENTES_CHAT_ID`
- `DATABASE_PATH` opcional. Use `cop_bot.db`.

## Importante no Railway
Para rodar bot e dashboard juntos no mesmo projeto, o mais simples é criar 2 serviços no Railway apontando para o mesmo repositório:

### Serviço 1 - Bot
Start Command:
```bash
python bot_cop_telegram.py
```

### Serviço 2 - Dashboard
Start Command:
```bash
gunicorn dashboard_app:app --bind 0.0.0.0:$PORT
```

Ambos devem usar o mesmo `DATABASE_PATH`.

Observação: SQLite no Railway pode perder dados se o container reiniciar sem volume persistente. Para produção, o ideal é PostgreSQL.
