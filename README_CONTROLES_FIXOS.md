# Controles sempre no final do tópico

Esta versão mantém uma única mensagem de controle no final de cada tópico:

- ✅ Finalizar atendimento
- 🔄 Devolver para fila

Como funciona:
- A mensagem anterior de controle é apagada.
- Uma nova é enviada após cada mensagem do técnico ou do COP.
- Assim, os botões permanecem sempre como a última mensagem do tópico.
- Ao finalizar ou devolver, a mensagem de controle é removida.
- Funciona separadamente para vários tópicos no mesmo grupo.

Substitua o `bot_cop_telegram.py` atual no GitHub e faça commit.
