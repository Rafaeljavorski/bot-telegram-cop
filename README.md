# Bot COP com PostgreSQL - driver corrigido

Esta versão usa `psycopg[binary]`, que não depende do `libpq.so.5` do Railway.

Substitua no GitHub:
- `bot_cop_telegram.py`
- `requirements.txt`

Depois faça commit e aguarde o Railway redeployar.
