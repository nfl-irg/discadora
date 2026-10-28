"""Campanhas da base propria: criar campanha e sortear lotes."""
import argparse
import psycopg
from psycopg.types.json import Jsonb

DSN_DISCADORA = "dbname=discadora"
DSN_MAILING = "host=192.168.56.1 dbname=db_mailing user=discadora_leitura"
FILTROS_PERMITIDOS = ("uf", "mesorregiao", "cidade", "tipo")


def obter_projeto(conn, nome, cliente="Interno"):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM clientes WHERE nome = %s", (cliente,))
        cliente_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO projetos (cliente_id, nome) VALUES (%s, %s)
                       ON CONFLICT (cliente_id, nome) DO UPDATE SET nome = EXCLUDED.nome
                       RETURNING id""", (cliente_id, nome))
        return cur.fetchone()[0]


def criar_campanha(conn, projeto, nome, filtro, quarentena, max_tentativas):
    projeto_id = obter_projeto(conn, projeto)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO campanhas
                         (projeto_id, nome, origem, filtro, quarentena_dias, max_tentativas)
                       VALUES (%s, %s, 'base', %s, %s, %s) RETURNING id""",
                    (projeto_id, nome, Jsonb(filtro), quarentena, max_tentativas))
        campanha_id = cur.fetchone()[0]
    conn.commit()
    return campanha_id


def sortear_lote(conn, campanha_id, quantidade):
    with conn.cursor() as cur:
        cur.execute("SELECT filtro, quarentena_dias FROM campanhas WHERE id = %s", (campanha_id,))
        filtro, quarentena = cur.fetchone()

    # monta o filtro so com campos permitidos (protege contra SQL injetado)
    condicoes, params = [], {"limite": quantidade}
    for campo in FILTROS_PERMITIDOS:
        if campo in filtro:
            condicoes.append(f"c.{campo} = %({campo})s")
            params[campo] = filtro[campo]
    if filtro.get("so_quentes"):
        condicoes.append("c.is_quente")
    if quarentena:
        condicoes.append("""NOT EXISTS (SELECT 1 FROM f_survey s
                                        WHERE s.telefone = c.telefone
                                          AND s.data >= current_date - %(quarentena)s)""")
        params["quarentena"] = quarentena
    where = " AND ".join(condicoes) or "true"

    consulta = f"""SELECT c.telefone, c.uf, c.cidade, c.mesorregiao, c.bairro, c.regional, c.tipo,
                          c.sexo, c.idade, c.escolaridade, c.renda, c.ocupacao
                   FROM vw_discadora_contatos c
                   WHERE {where}
                   ORDER BY random()
                   LIMIT %(limite)s"""

    with psycopg.connect(DSN_MAILING) as mailing, mailing.cursor() as cur:
        cur.execute(consulta, params)
        colunas = [d.name for d in cur.description]
        linhas = cur.fetchall()

    # bloqueios feitos na tabulacao (ainda nao levados para a d_blocklist)
    with conn.cursor() as cur:
        cur.execute("SELECT telefone FROM bloqueios")
        bloqueados_locais = {r[0] for r in cur.fetchall()}
    linhas = [l for l in linhas if l[0] not in bloqueados_locais]

    with conn.cursor() as cur:
        cur.execute("""INSERT INTO lotes (campanha_id, solicitados, inseridos, regras)
                       VALUES (%s, %s, 0, %s) RETURNING id""",
                    (campanha_id, quantidade,
                     Jsonb({"filtro": filtro, "quarentena_dias": quarentena})))
        lote_id = cur.fetchone()[0]
        inseridos = 0
        for linha in linhas:
            reg = dict(zip(colunas, linha))
            dados = {k: v for k, v in reg.items()
                     if k not in ("telefone", "uf", "cidade") and v is not None}
            cur.execute("""INSERT INTO campanha_contatos
                             (campanha_id, lote_id, telefone, uf, cidade, dados)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (campanha_id, telefone) DO NOTHING""",
                        (campanha_id, lote_id, reg["telefone"], reg["uf"],
                         reg["cidade"], Jsonb(dados)))
            inseridos += cur.rowcount
        cur.execute("UPDATE lotes SET inseridos = %s WHERE id = %s", (inseridos, lote_id))
    conn.commit()
    return lote_id, len(linhas), inseridos


def main():
    p = argparse.ArgumentParser(description="Campanhas da base propria")
    sub = p.add_subparsers(dest="comando", required=True)

    c = sub.add_parser("criar", help="cria a campanha e sorteia o primeiro lote")
    c.add_argument("--projeto", required=True)
    c.add_argument("--nome", required=True)
    c.add_argument("--uf", required=True)
    c.add_argument("--cidade")
    c.add_argument("--mesorregiao")
    c.add_argument("--tipo", choices=["CELULAR", "FIXO"])
    c.add_argument("--so-quentes", action="store_true")
    c.add_argument("--quarentena", type=int, help="dias; omita para nao aplicar")
    c.add_argument("--tentativas", type=int, default=3)
    c.add_argument("--lote", type=int, default=5000)

    s = sub.add_parser("sortear", help="sorteia mais um lote para uma campanha")
    s.add_argument("--campanha", type=int, required=True)
    s.add_argument("--lote", type=int, default=5000)

    a = p.parse_args()
    with psycopg.connect(DSN_DISCADORA) as conn:
        if a.comando == "criar":
            filtro = {"uf": a.uf.upper()}
            if a.cidade:      filtro["cidade"] = a.cidade.upper()
            if a.mesorregiao: filtro["mesorregiao"] = a.mesorregiao.upper()
            if a.tipo:        filtro["tipo"] = a.tipo
            if a.so_quentes:  filtro["so_quentes"] = True
            campanha_id = criar_campanha(conn, a.projeto, a.nome, filtro,
                                         a.quarentena, a.tentativas)
            print(f"Campanha {campanha_id} criada | filtro {filtro} | "
                  f"quarentena {a.quarentena or 'nenhuma'}")
        else:
            campanha_id = a.campanha
        lote_id, sorteados, inseridos = sortear_lote(conn, campanha_id, a.lote)
        print(f"Lote {lote_id}: {sorteados} sorteados, {inseridos} novos na fila "
              f"da campanha {campanha_id}")


if __name__ == "__main__":
    main()
