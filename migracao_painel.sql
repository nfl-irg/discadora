CREATE TABLE usuarios (
    id         serial PRIMARY KEY,
    login      text NOT NULL UNIQUE,
    nome       text NOT NULL,
    senha_hash text NOT NULL,
    perfil     text NOT NULL
               CHECK (perfil IN ('admin', 'supervisor', 'monitoria', 'operador', 'cliente')),
    ramal      text,                              -- para operadores
    cliente_id int REFERENCES clientes(id),       -- para usuarios de cliente
    ativo      boolean NOT NULL DEFAULT true,
    criado_em  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auditoria (
    id         bigserial PRIMARY KEY,
    usuario_id int REFERENCES usuarios(id),
    acao       text NOT NULL,
    detalhe    jsonb NOT NULL DEFAULT '{}',
    ip         text,
    quando     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_auditoria_quando ON auditoria (quando);
