"""Importa um mailing CSV enviado por cliente para a discadora."""
import argparse, csv, io, re, unicodedata
from pathlib import Path
import psycopg
from psycopg.types.json import Jsonb

DSN_DISCADORA = "dbname=discadora"
DSN_MAILING = "host=192.168.56.1 dbname=db_mailing user=discadora_leitura"

DDDS = {11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68,
        69, 71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95,
        96, 97, 98, 99}


def normalizar_telefone(bruto):
    """Devolve 10 digitos (fixo) ou 11 (celular), ou None se invalido."""
    d = re.sub(r"\D", "", bruto or "").lstrip("0")
    if len(d) in (12, 13) and d.startswith("55"):      # codigo do pais
        d = d[2:]
    if len(d) == 10 and d[2] in "6789":                 # celular antigo, sem o 9
        d = d[:2] + "9" + d[2:]
    if len(d) not in (10, 11) or int(d[:2]) not in DDDS:
        return None
    if len(d) == 11 and d[2] != "9":
        return None
    if len(d) == 10 and d[2] not in "2345":
        return None
    return d


def normalizar_texto(s):
    """Tira acentos, espacos repetidos e passa para maiusculas."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper() or None


def ler_csv(caminho):
    bruto = Path(caminho).read_bytes()
    try:
        texto, codificacao = bruto.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        texto, codificacao = bruto.decode("cp1252"), "cp1252"   # Excel no Windows
    dialeto = csv.Sniffer().sniff(texto[:5000], delimiters=";,\t|")
    leitor = csv.DictReader(io.StringIO(texto, newline=""), dialect=dialeto)
    return leitor.fieldnames, list(leitor), codificacao, dialeto.delimiter


def main():
    p = argparse.ArgumentParser(description="Importa mailing CSV de cliente")
    p.add_argument("arquivo")
    p.add_argument("--cliente", required=True)
    p.add_argument("--projeto", required=True)
    p.add_argument("--nome", help="nome da lista (padrao: nome do arquivo)")
    p.add_argument("--telefones", required=True,
                   help="colunas de telefone separadas por virgula, ex.: tel1,tel2")
    p.add_argument("--uf", help="coluna da UF")
    p.add_argument("--cidade", help="coluna da cidade")
    a = p.parse_args()

    colunas, linhas, codificacao, separador = ler_csv(a.arquivo)
    cols_tel = [c.strip() for c in a.telefones.split(",")]
    faltando = [c for c in cols_tel + [a.uf, a.cidade] if c and c not in colunas]
    if faltando:
        raise SystemExit(f"Colunas nao encontradas: {faltando}\nColunas do arquivo: {colunas}")

    # 1) normaliza telefones e marca invalidos e duplicados
    cont = {"invalidos": 0, "duplicados": 0, "bloqueados": 0}
    vistos, registros = set(), []
    for n, linha in enumerate(linhas, start=2):            # linha 1 = cabecalho
        tels, problemas = [], []
        for col in cols_tel:
            bruto = (linha.get(col) or "").strip()
            if not bruto:
                continue
            t = normalizar_telefone(bruto)
            if t is None:
                problemas.append(f"invalido:{col}"); cont["invalidos"] += 1
            elif t in vistos:
                problemas.append(f"duplicado:{col}"); cont["duplicados"] += 1
            else:
                vistos.add(t); tels.append(t)
        registros.append({"linha": n, "dados": linha, "tels": tels, "problemas": problemas})

    # 2) consulta blocklist e apelidos de cidade no seu banco
    with psycopg.connect(DSN_MAILING) as m, m.cursor() as cur:
        cur.execute("SELECT telefone_norm FROM d_blocklist WHERE telefone_norm = ANY(%s)",
                    (list(vistos),))
        bloqueados = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT cidade_errada, cidade_correta FROM d_cidade_alias")
        apelidos = dict(cur.fetchall())

    validos = 0
    for r in registros:
        for t in [t for t in r["tels"] if t in bloqueados]:
            r["tels"].remove(t); r["problemas"].append("bloqueado"); cont["bloqueados"] += 1
        validos += len(r["tels"])
        r["uf"] = normalizar_texto(r["dados"].get(a.uf)) if a.uf else None
        cidade = normalizar_texto(r["dados"].get(a.cidade)) if a.cidade else None
        r["cidade"] = apelidos.get(cidade, cidade)

    # 3) grava cliente, projeto, lista e todas as linhas
    with psycopg.connect(DSN_DISCADORA) as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO clientes (nome) VALUES (%s)
                           ON CONFLICT (nome) DO UPDATE SET nome = EXCLUDED.nome
                           RETURNING id""", (a.cliente,))
            cliente_id = cur.fetchone()[0]
            cur.execute("""INSERT INTO projetos (cliente_id, nome) VALUES (%s, %s)
                           ON CONFLICT (cliente_id, nome) DO UPDATE SET nome = EXCLUDED.nome
                           RETURNING id""", (cliente_id, a.projeto))
            projeto_id = cur.fetchone()[0]
            cur.execute("""INSERT INTO listas (projeto_id, nome, arquivo, linhas,
                             telefones_validos, invalidos, duplicados, bloqueados)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (projeto_id, a.nome or Path(a.arquivo).stem, Path(a.arquivo).name,
                         len(registros), validos, cont["invalidos"], cont["duplicados"],
                         cont["bloqueados"]))
            lista_id = cur.fetchone()[0]
            cur.execute("UPDATE listas SET colunas = %s WHERE id = %s", (colunas, lista_id))
            cur.executemany("""INSERT INTO lista_contatos
                                 (lista_id, linha, uf, cidade, dados, telefones, problemas)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            [(lista_id, r["linha"], r["uf"], r["cidade"], Jsonb(r["dados"]),
                              r["tels"], r["problemas"]) for r in registros])
        conn.commit()

    sem_telefone = sum(1 for r in registros if not r["tels"])
    print(f"Arquivo {a.arquivo} (codificacao {codificacao}, separador '{separador}')")
    print(f"Lista {lista_id} importada | cliente '{a.cliente}' | projeto '{a.projeto}'")
    print(f"  linhas no arquivo        : {len(registros)}")
    print(f"  telefones validos        : {validos}")
    print(f"  invalidos                : {cont['invalidos']}")
    print(f"  duplicados               : {cont['duplicados']}")
    print(f"  bloqueados               : {cont['bloqueados']}")
    print(f"  linhas sem tel. discavel : {sem_telefone}")


if __name__ == "__main__":
    main()
