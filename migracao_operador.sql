-- ramais WebRTC dos operadores (a senha SIP fica so no banco e no Asterisk)
CREATE TABLE ramais (
    ramal     text PRIMARY KEY,
    senha_sip text NOT NULL
);

-- configuracao da tela do operador, por campanha
ALTER TABLE campanhas
  ADD COLUMN tabulacao_ativa boolean NOT NULL DEFAULT false,
  ADD COLUMN tabulacoes jsonb NOT NULL DEFAULT
    '[{"codigo": "concluida", "nome": "Concluída"},
      {"codigo": "recusou",   "nome": "Recusou"},
      {"codigo": "fora_cota", "nome": "Fora da cota"},
      {"codigo": "nao_ligar", "nome": "Pediu para não ser mais ligado", "bloqueia": true}]',
  ADD COLUMN campos_tela jsonb NOT NULL DEFAULT '[]',   -- [{"campo": "cidade", "rotulo": "Cidade"}, ...]
  ADD COLUMN questionario_url text;

-- o que cada operador esta fazendo agora
ALTER TABLE operadores_online
  ADD COLUMN estado text NOT NULL DEFAULT 'livre',      -- livre | em_chamada | tabulando
  ADD COLUMN tentativa_atual bigint;

-- pedidos de "nao ligar" feitos na tabulacao (exportados depois para a d_blocklist)
CREATE TABLE bloqueios (
    telefone     text PRIMARY KEY,
    motivo       text,
    origem       text,
    criado_em    timestamptz NOT NULL DEFAULT now(),
    exportado_em timestamptz
);
