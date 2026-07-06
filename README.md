# Bot COP - fluxo NAP atualizado

Alterações:
- Ao clicar em NAP, aparecem 3 opções:
  - Localização da NAP
  - NAP mais próxima
  - ID da NAP
- Se escolher NAP mais próxima ou ID da NAP:
  - solicita contrato;
  - depois solicita localização;
  - não pede fotos;
  - envia direto para a fila após receber localização.
- Localização da NAP segue fluxo normal com evidências/fotos.
- Mantém PostgreSQL e demais correções.
