# Teste Rafael no grupo Supervisor

Configuração incluída:

```python
COP_GROUPS = {
    8176848972: -1005562485186,  # Eduardo
    8342651270: -1005133624770,  # Rafael - teste no grupo Supervisor
}
```

Como testar:
1. Garanta que o grupo SUPERVISOR está com tópicos ativados.
2. Garanta que o bot é administrador no SUPERVISOR e pode gerenciar tópicos.
3. Rafael clica em "Atender próximo" no grupo principal.
4. O tópico deve ser criado no grupo SUPERVISOR.

Também adicionei o comando:
`/debug`

Use dentro de qualquer grupo para conferir:
- Chat ID
- Se é fórum
- Seu ID
