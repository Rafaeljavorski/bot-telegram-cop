# Bot COP - mensagens por opção

Alterações:
- Ativo > Cliente ausente:
  - pede localização;
  - depois pede foto da fachada;
  - entra na fila.
- Ativo > Endereço não localizado:
  - pede localização;
  - depois pede foto da placa da rua ou numeração mais próxima;
  - entra na fila.
- Ativo > Outros:
  - vai direto para atendimento com COP após contrato.
- Atenuação:
  - pergunta se vai informar Numeração da NAP ou Localização da NAP;
  - Numeração da NAP: pede texto e entra na fila;
  - Localização da NAP: pede localização e entra na fila.
- Mantém fluxos anteriores de PSW, NAP, PostgreSQL e travas.
