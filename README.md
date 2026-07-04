# Bot COP Telegram - CIP Telecom

Bot Telegram criado com base no fluxo do COP.

## Funções incluídas

- Menu principal com PSW, NAP, Ativo, Atenuação, Outros e Finalizar atendimento
- Coleta de contrato
- Botão para envio de localização GPS
- Recebimento de fotos
- Alerta de obrigatoriedade PSW
- Fila para grupo de atendentes
- Botão de assumir atendimento
- Mensagem automática para o técnico com nome do atendente
- Botão de finalizar chamado

## Como configurar

1. Crie um bot no Telegram pelo @BotFather.
2. Copie o token gerado.
3. Crie um grupo no Telegram para os atendentes/COP.
4. Adicione o bot nesse grupo.
5. Descubra o ID do grupo.
6. Copie `.env.example` para `.env`.
7. Preencha:

```env
TELEGRAM_BOT_TOKEN=seu_token_aqui
ATENDENTES_CHAT_ID=id_do_grupo_aqui
```

## Como instalar

```bash
pip install -r requirements.txt
```

## Como rodar

```bash
python bot_cop_telegram.py
```

## Observação importante

Nesta versão, os chamados ficam salvos apenas enquanto o bot estiver ligado.
Para uso definitivo, o ideal é salvar em planilha Google, banco SQLite ou servidor.
