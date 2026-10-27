-- ================= troncos =================
CREATE TABLE troncos (
    id            serial PRIMARY KEY,
    nome          text NOT NULL UNIQUE,
    endpoint      text NOT NULL,       -- modelo: 'PJSIP/{numero}@algar1' (no lab: 'PJSIP/{numero}')
    canais        int  NOT NULL,
    limite_diario int  NOT NULL,
    prioridade    int  NOT NULL DEFAULT 1,
    ativo         boolean NOT NULL DEFAULT true
);

-- ================= configuracoes gerais =================
CREATE TABLE config (
    chave text PRIMARY KEY,
    valor text NOT NULL
);
INSERT INTO config VALUES
  ('limite_diario_empresa', '95000'),
  ('reserva_call_modo',     'automatico'),   -- 'automatico' ou 'fixo'
  ('reserva_call_ritmo',    '2'),
  ('reserva_call_folga',    '2'),
  ('reserva_call_fixa',     '30');

-- ================= placar em tempo real =================
CREATE TABLE canais_em_uso (
    canal       text PRIMARY KEY,                      -- id do canal no Asterisk
    tronco_id   int  NOT NULL REFERENCES troncos(id),
    tipo        text NOT NULL CHECK (tipo IN ('call', 'ura')),
    campanha_id int,
    desde       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE operadores_online (
    canal       text PRIMARY KEY,
    ramal       text NOT NULL,
    campanha_id int,
    desde       timestamptz NOT NULL DEFAULT now()
);

-- ================= ajustes nas tabelas existentes =================
ALTER TABLE campanhas  ADD COLUMN tipo text NOT NULL DEFAULT 'call' CHECK (tipo IN ('call', 'ura'));
ALTER TABLE campanhas  ADD COLUMN max_canais int;           -- teto de canais da campanha (URA)
ALTER TABLE tentativas ADD COLUMN tronco_id int REFERENCES troncos(id);
CREATE INDEX ix_tentativas_tronco_dia ON tentativas (tronco_id, inicio);
CREATE INDEX ix_tentativas_inicio ON tentativas (inicio);

-- ================= reserva de canal =================
-- devolve o id do tronco reservado, ou NULL se nao houver canal disponivel
CREATE FUNCTION reservar_canal(p_canal text, p_tipo text, p_campanha int)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
    v_tronco      int;
    v_ops         int;
    v_reserva     int := 0;
    v_total       int;
    v_em_uso      int;
    v_em_uso_call int;
BEGIN
    -- um pedido por vez (fila), mesmo vindo de programas diferentes
    PERFORM pg_advisory_xact_lock(4242);

    -- 1) limite diario da empresa (URA + call)
    IF (SELECT count(*) FROM tentativas WHERE inicio >= current_date)
         >= (SELECT valor::int FROM config WHERE chave = 'limite_diario_empresa') THEN
        RETURN NULL;
    END IF;

    -- 2) prioridade do call: a URA respeita a reserva dos operadores logados
    IF p_tipo = 'ura' THEN
        SELECT count(*) INTO v_ops FROM operadores_online;
        IF v_ops > 0 THEN
            IF (SELECT valor FROM config WHERE chave = 'reserva_call_modo') = 'fixo' THEN
                v_reserva := (SELECT valor::int FROM config WHERE chave = 'reserva_call_fixa');
            ELSE
                v_reserva := ceil(v_ops * (SELECT valor::numeric FROM config
                                           WHERE chave = 'reserva_call_ritmo'))::int
                             + (SELECT valor::int FROM config WHERE chave = 'reserva_call_folga');
            END IF;
        END IF;
        SELECT coalesce(sum(canais), 0) INTO v_total FROM troncos WHERE ativo;
        SELECT count(*) INTO v_em_uso FROM canais_em_uso;
        SELECT count(*) INTO v_em_uso_call FROM canais_em_uso WHERE tipo = 'call';
        IF v_total - v_em_uso - greatest(v_reserva - v_em_uso_call, 0) <= 0 THEN
            RETURN NULL;
        END IF;
    END IF;

    -- 3) escolhe o tronco: ativo, com canal livre e saldo na trava diaria;
    --    entre os possiveis, o de menor prioridade e o menos ocupado
    SELECT t.id INTO v_tronco
    FROM troncos t
    WHERE t.ativo
      AND (SELECT count(*) FROM canais_em_uso c WHERE c.tronco_id = t.id) < t.canais
      AND (SELECT count(*) FROM tentativas x
           WHERE x.tronco_id = t.id AND x.inicio >= current_date) < t.limite_diario
    ORDER BY t.prioridade,
             (SELECT count(*) FROM canais_em_uso c WHERE c.tronco_id = t.id)::numeric / t.canais
    LIMIT 1;

    IF v_tronco IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO canais_em_uso (canal, tronco_id, tipo, campanha_id)
    VALUES (p_canal, v_tronco, p_tipo, p_campanha);
    RETURN v_tronco;
END $$;

-- ================= troncos simulados do laboratorio =================
INSERT INTO troncos (nome, endpoint, canais, limite_diario) VALUES
  ('lab-a', 'PJSIP/{numero}', 1, 20),
  ('lab-b', 'PJSIP/{numero}', 1, 20);
