-- rodada atual de cada campanha
ALTER TABLE campanhas ADD COLUMN rodada int NOT NULL DEFAULT 1;

-- duracao minima (segundos) antes de a plataforma desligar uma chamada atendida
INSERT INTO config VALUES ('duracao_minima_s', '8');

-- entrega o proximo contato respeitando as rodadas (usada pela URA e pela discadora)
CREATE FUNCTION pegar_contato(p_campanha int)
RETURNS TABLE (id bigint, telefone text, tentativas int, lista_contato_id bigint)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    v_rodada int;
    v_max    int;
BEGIN
    SELECT c.rodada, c.max_tentativas INTO v_rodada, v_max
    FROM campanhas c WHERE c.id = p_campanha;

    -- avanca a rodada quando ninguem da rodada atual esta pendente ou em ligacao
    IF v_rodada < v_max AND NOT EXISTS (
        SELECT 1 FROM campanha_contatos x
        WHERE x.campanha_id = p_campanha
          AND x.status IN ('pendente', 'discando')
          AND x.tentativas < v_rodada) THEN
        UPDATE campanhas c SET rodada = c.rodada + 1
        WHERE c.id = p_campanha AND c.rodada = v_rodada;
        v_rodada := v_rodada + 1;
    END IF;

    RETURN QUERY
    UPDATE campanha_contatos cc SET status = 'discando', atualizado_em = now()
    WHERE cc.id = (SELECT x.id FROM campanha_contatos x
                   WHERE x.campanha_id = p_campanha
                     AND x.status = 'pendente'
                     AND x.tentativas < v_rodada
                     AND x.proxima_tentativa <= now()
                   ORDER BY x.tentativas, x.proxima_tentativa, x.id
                   LIMIT 1 FOR UPDATE SKIP LOCKED)
    RETURNING cc.id, cc.telefone, cc.tentativas, cc.lista_contato_id;
END $$;

-- reciclar: +1 chamada para todos os finalizados que nao atenderam
CREATE FUNCTION reciclar_campanha(p_campanha int)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
    v_novo_max int;
    v_qtd      int;
BEGIN
    UPDATE campanhas SET max_tentativas = max_tentativas + 1
    WHERE id = p_campanha
    RETURNING max_tentativas INTO v_novo_max;

    UPDATE campanhas SET rodada = v_novo_max WHERE id = p_campanha;

    UPDATE campanha_contatos
    SET status = 'pendente', proxima_tentativa = now(), atualizado_em = now()
    WHERE campanha_id = p_campanha
      AND status = 'finalizado'
      AND split_part(coalesce(ultimo_resultado, ''), ' (', 1)
          NOT IN ('atendida', 'respondente', 'invalido', 'atendido em outro telefone');
    GET DIAGNOSTICS v_qtd = ROW_COUNT;
    RETURN v_qtd;
END $$;
