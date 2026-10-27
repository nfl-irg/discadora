-- audios gerais da campanha de URA (caminho dentro de /var/lib/asterisk/sounds, sem extensao)
ALTER TABLE campanhas ADD COLUMN ura_abertura text,
                      ADD COLUMN ura_encerramento text,
                      ADD COLUMN ura_invalida text;

-- roteiro: uma linha por pergunta
CREATE TABLE ura_perguntas (
    campanha_id    int  NOT NULL REFERENCES campanhas(id),
    ordem          int  NOT NULL CHECK (ordem BETWEEN 1 AND 12),
    audio          text NOT NULL,            -- ex.: 'ura/lab/p1'
    teclas_validas text NOT NULL,            -- ex.: '123'
    timeout_s      int  NOT NULL DEFAULT 7,  -- segundos esperando a tecla
    PRIMARY KEY (campanha_id, ordem)
);

-- cada tecla respondida, gravada na hora
CREATE TABLE ura_respostas (
    tentativa_id  bigint NOT NULL REFERENCES tentativas(id),
    ordem         int    NOT NULL,
    tecla         text   NOT NULL,
    respondida_em timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tentativa_id, ordem)
);

-- resultado da deteccao de secretaria eletronica
ALTER TABLE tentativas ADD COLUMN amd_status text, ADD COLUMN amd_causa text;

-- novos resultados e o que fazer com eles
INSERT INTO regras_rediscagem VALUES
  ('respondente',  true,  NULL,      true),
  ('caixa_postal', false, '4 hours', true);
