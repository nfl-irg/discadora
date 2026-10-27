-- ===================== donos =====================
CREATE TABLE clientes (
    id        serial PRIMARY KEY,
    nome      text NOT NULL UNIQUE,
    criado_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projetos (
    id         serial PRIMARY KEY,
    cliente_id int  NOT NULL REFERENCES clientes(id),
    nome       text NOT NULL,
    criado_em  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (cliente_id, nome)
);

-- ============ mailings enviados por clientes (CSV) ============
CREATE TABLE listas (
    id                serial PRIMARY KEY,
    projeto_id        int  NOT NULL REFERENCES projetos(id),
    nome              text NOT NULL,
    arquivo           text,
    importada_em      timestamptz NOT NULL DEFAULT now(),
    linhas            int,
    telefones_validos int,
    invalidos         int,
    duplicados        int,
    bloqueados        int
);

CREATE TABLE lista_contatos (
    id       bigserial PRIMARY KEY,
    lista_id int NOT NULL REFERENCES listas(id) ON DELETE CASCADE,
    linha    int NOT NULL,                  -- linha no arquivo original
    uf       text,
    cidade   text,
    dados    jsonb NOT NULL DEFAULT '{}'    -- colunas extras do cliente
);

-- ======================== campanhas ========================
CREATE TABLE campanhas (
    id              serial PRIMARY KEY,
    projeto_id      int  NOT NULL REFERENCES projetos(id),
    nome            text NOT NULL,
    origem          text NOT NULL CHECK (origem IN ('base', 'lista')),
    lista_id        int  REFERENCES listas(id),
    filtro          jsonb NOT NULL DEFAULT '{}',   -- ex.: {"uf":"PR","cidade":"CURITIBA"}
    quarentena_dias int,                           -- NULL = sem quarentena
    max_tentativas  int  NOT NULL DEFAULT 3,
    janela_inicio   time NOT NULL DEFAULT '09:00',
    janela_fim      time NOT NULL DEFAULT '20:00',
    status          text NOT NULL DEFAULT 'rascunho'
                    CHECK (status IN ('rascunho', 'ativa', 'pausada', 'encerrada')),
    criada_em       timestamptz NOT NULL DEFAULT now(),
    CHECK (origem = 'base' OR lista_id IS NOT NULL)
);

-- cada sorteio na base propria fica registrado
CREATE TABLE lotes (
    id          serial PRIMARY KEY,
    campanha_id int NOT NULL REFERENCES campanhas(id),
    sorteado_em timestamptz NOT NULL DEFAULT now(),
    solicitados int NOT NULL,
    inseridos   int NOT NULL,
    regras      jsonb NOT NULL
);

-- a fila: um telefone por linha
CREATE TABLE campanha_contatos (
    id                bigserial PRIMARY KEY,
    campanha_id       int  NOT NULL REFERENCES campanhas(id),
    lote_id           int  REFERENCES lotes(id),
    lista_contato_id  bigint REFERENCES lista_contatos(id),
    telefone          text NOT NULL,
    ordem_telefone    smallint NOT NULL DEFAULT 1,   -- tel1, tel2, tel3...
    uf                text,
    cidade            text,
    dados             jsonb NOT NULL DEFAULT '{}',
    status            text NOT NULL DEFAULT 'pendente'
                      CHECK (status IN ('pendente', 'discando', 'finalizado')),
    tentativas        int  NOT NULL DEFAULT 0,
    proxima_tentativa timestamptz NOT NULL DEFAULT now(),
    ultimo_resultado  text,
    atualizado_em     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campanha_id, telefone)
);
CREATE INDEX ix_fila ON campanha_contatos (campanha_id, status, proxima_tentativa);

-- cada ligacao feita
CREATE TABLE tentativas (
    id                  bigserial PRIMARY KEY,
    campanha_id         int    NOT NULL REFERENCES campanhas(id),
    campanha_contato_id bigint NOT NULL REFERENCES campanha_contatos(id),
    telefone            text   NOT NULL,
    canal               text,              -- id do canal = nome da gravacao
    operador            text,
    inicio              timestamptz NOT NULL DEFAULT now(),
    atendida_em         timestamptz,
    fim                 timestamptz,
    resultado           text,
    causa_codigo        int,
    causa_texto         text,
    conversa_s          int,
    tabulacao           text               -- o que o operador marcou (proxima fase)
);
CREATE INDEX ix_tentativas ON tentativas (campanha_id, inicio);

-- ================ regras de rediscagem ================
CREATE TABLE regras_rediscagem (
    resultado        text PRIMARY KEY,
    finaliza         boolean  NOT NULL,   -- true = nunca mais discar
    intervalo        interval,            -- espera ate a proxima tentativa
    conta_tentativa  boolean  NOT NULL DEFAULT true
);

INSERT INTO regras_rediscagem VALUES
  ('atendida',        true,  NULL,        true),
  ('invalido',        true,  NULL,        true),
  ('ocupado',         false, '30 min',    true),
  ('nao_atende',      false, '2 hours',   true),
  ('recusou',         false, '1 day',     true),
  ('indisponivel',    false, '4 hours',   true),
  ('falha_rede',      false, '5 min',     false),
  ('erro_originacao', false, '10 min',    false),
  ('abandono',        false, '1 hour',    false),
  ('outro',           false, '1 hour',    true);

INSERT INTO clientes (nome) VALUES ('Interno');
