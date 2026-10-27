"""Cria uma campanha a partir de uma lista importada (CSV de cliente)."""
import argparse
import psycopg

DSN_DISCADORA = "dbname=discadora"
DSN_MAILING = "host=192.168.56.1 dbname=db_mailing user=discadora_leitura"


def main():
    p = argparse.ArgumentParser(description="Cria campanha a partir de uma lista importada")
    p.add_argument("--lista", type=int, required=True)
    p.add_argument("--nome", required=True)
    p.add_argument("--quarentena", type=int, help="dias; omita para nao aplicar")
    p.add_argument("--tentativas", type=int, default=3)
    a = p.parse_args()

    with psycopg.connect(DSN_DISCADORA) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT projeto_id FROM listas WHERE id = %s", (a.lista,))
            achou = cur.fetchone()
            if not achou:
                raise SystemExit(f"Lista {a.lista} nao existe")
            projeto_id = achou[0]
            cur.execute("SELECT DISTINCT unnest(telefones) FROM lista_contatos WHERE lista_id = %s",
                        (a.lista,))
            telefones = [r[0] for r in cur.fetchall()]

        # quarentena: quem foi entrevistado nos ultimos N dias (consulta no seu banco)
        em_quarentena = set()
        if a.quarentena:
            with psycopg.connect(DSN_MAILING) as m, m.cursor() as cur:
                cur.execute("""SELECT DISTINCT telefone FROM f_survey
                               WHERE telefone = ANY(%s) AND data >= current_date - %s""",
                            (telefones, a.quarentena))
                em_quarentena = {r[0] for r in cur.fetchall()}

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO campanhas
                             (projeto_id, nome, origem, lista_id, quarentena_dias, max_tentativas)
                           VALUES (%s, %s, 'lista', %s, %s, %s) RETURNING id""",
                        (projeto_id, a.nome, a.lista, a.quarentena, a.tentativas))
            campanha_id = cur.fetchone()[0]
            # um telefone por linha na fila, guardando a ordem (tel1, tel2...)
            cur.execute("""INSERT INTO campanha_contatos
                             (campanha_id, lista_contato_id, telefone, ordem_telefone,
                              uf, cidade, dados)
                           SELECT %s, lc.id, t.tel, t.ordem, lc.uf, lc.cidade, lc.dados
                           FROM lista_contatos lc
                           CROSS JOIN LATERAL unnest(lc.telefones) WITH ORDINALITY AS t(tel, ordem)
                           WHERE lc.lista_id = %s AND NOT (t.tel = ANY(%s))
                           ON CONFLICT (campanha_id, telefone) DO NOTHING""",
                        (campanha_id, a.lista, list(em_quarentena)))
            na_fila = cur.rowcount
        conn.commit()

    print(f"Campanha {campanha_id} criada a partir da lista {a.lista}")
    print(f"  telefones na lista : {len(telefones)}")
    print(f"  em quarentena      : {len(em_quarentena)}"
          + ("" if a.quarentena else " (quarentena nao aplicada)"))
    print(f"  na fila            : {na_fila}")


if __name__ == "__main__":
    main()
