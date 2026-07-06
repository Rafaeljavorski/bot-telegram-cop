# Bot COP - teste com grupos separados

Configuração aplicada:

```python
COP_GROUPS = {
    8176848972: -1005562485186,  # Eduardo
}

SUPERVISOR_GROUP_ID = -1005133624770
SUPERVISOR_IDS = [8342651270]
```

Como testar:
1. Substitua `bot_cop_telegram.py` no GitHub.
2. Mantenha o mesmo `requirements.txt`.
3. Faça commit e aguarde o Railway atualizar.
4. Abra um chamado como técnico.
5. No grupo principal da fila, o Eduardo clica em **Atender próximo**.
6. O tópico deve ser criado no grupo **EDUARDO BOT**.
7. Uma cópia deve ser criada no grupo **SUPERVISOR**.

Observações:
- Por enquanto apenas Eduardo está configurado com grupo individual.
- O grupo principal fica somente com a fila.
- O grupo Supervisor é apenas acompanhamento neste teste.
