"""Relatorio de retorno de uma campanha, em CSV pronto para o Excel."""
import argparse, csv
from datetime import datetime
import psycopg
from psycopg.rows import dict_row

NOMES = {"atendida": "Atendido", "nao_atende": "Não atende", "ocupado": "Ocupado",
         "recusou": "Recusou", "indisponivel": "Desligado/fora de área",
         "invalido": "Número inválido", "falha_rede": "Falha de rede",
         "erro_originacao": "Erro técnico", "abandono": "Abandono",
         "falha_tecnica": "Falha técnica", "outro": "Outro",
         "atendido em outro telefone": "Atendido em outro telefone"}


def humano(ultimo):
    if not ultimo:
        return "Aguardando"
    base, _, resto = ultimo.partition(" (")
    return NOMES.get(base, base) + (" (tentativas esgotadas)" if "esgotadas" in resto else "")


def fones_da_campanha(cur, campanha_id):
    cur.execute("""SELECT cc.id, cc.lista_contato_id, cc.telefone, cc.status,
                          cc.ultimo_resultado, cc.uf, cc.cidade, cc.dados,
                          count(t.id) AS feitas, max(t.inicio) AS ultima,
                          coalesce(bool_or(t.atendida_em IS NOT NULL), false) AS atendeu
                   FROM campanha_contatos cc
                   LEFT JOIN tentativas t ON t.campanha_contato_id = cc.id
                   WHERE cc.campanha_id = %s
                   GROUP BY cc.id
                   ORDER BY cc.lista_contato_id, cc.ordem_telefone, cc.id""", (campanha_id,))
    return cur.fetchall()


def data(dt):
    return dt.strftime("%d/%m/%Y %H:%M") if dt else ""


def relatorio_lista(cur, campanha, saida):
    cur.execute("SELECT colunas FROM listas WHERE id = %s", (campanha["lista_id"],))
    colunas = cur.fetchone()["colunas"] or []
    cur.execute("""SELECT id, linha, dados, telefones, problemas FROM lista_contatos
                   WHERE lista_id = %s ORDER BY linha""", (campanha["lista_id"],))
    linhas = cur.fetchall()
    if not colunas and linhas:
        colunas = list(linhas[0]["dados"].keys())
    por_contato = {}
    for f in fones_da_campanha(cur, campanha["id"]):
        por_contato.setdefault(f["lista_contato_id"], []).append(f)

    with open(saida, "w", newline="", encoding="utf-8-sig") as arq:
        w = csv.writer(arq, delimiter=";")
        w.writerow(["linha_arquivo"] + colunas +
                   ["resultado", "detalhe", "telefone_atendido", "tentativas", "ultima_tentativa"])
        for lc in linhas:
            fones = por_contato.get(lc["id"], [])
            atendido = next((x for x in fones if x["atendeu"]), None)
            if not lc["telefones"]:
                resultado, motivo = "NÃO DISCADO", ", ".join(lc["problemas"]) or "sem telefone"
            elif atendido:
                resultado, motivo = "ATENDIDO", ""
            elif not fones:
                resultado, motivo = "NÃO DISCADO", "quarentena"
            elif any(x["status"] != "finalizado" for x in fones):
                resultado, motivo = "EM ANDAMENTO", ""
            else:
                resultado, motivo = "NÃO CONTATADO", ""
            partes = [f"{x['telefone']}: {humano(x['ultimo_resultado'])} ({x['feitas']}x)"
                      for x in fones]
            detalhe = " | ".join(filter(None, [motivo] + partes))
            ultima = max((x["ultima"] for x in fones if x["ultima"]), default=None)
            w.writerow([lc["linha"]] + [lc["dados"].get(c, "") for c in colunas] +
                       [resultado, detalhe, atendido["telefone"] if atendido else "",
                        sum(x["feitas"] for x in fones), data(ultima)])
    return len(linhas)


def relatorio_base(cur, campanha, saida):
    fones = fones_da_campanha(cur, campanha["id"])
    with open(saida, "w", newline="", encoding="utf-8-sig") as arq:
        w = csv.writer(arq, delimiter=";")
        w.writerow(["telefone", "uf", "cidade", "bairro", "resultado", "tentativas",
                    "ultima_tentativa", "situacao"])
        for x in fones:
            w.writerow([x["telefone"], x["uf"] or "", x["cidade"] or "",
                        (x["dados"] or {}).get("bairro", ""), humano(x["ultimo_resultado"]),
                        x["feitas"], data(x["ultima"]), x["status"]])
    return len(fones)


def main():
    p = argparse.ArgumentParser(description="Relatorio de retorno de uma campanha")
    p.add_argument("--campanha", type=int, required=True)
    p.add_argument("--saida", help="arquivo CSV de saida")
    a = p.parse_args()
    with psycopg.connect("dbname=discadora", row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM campanhas WHERE id = %s", (a.campanha,))
        campanha = cur.fetchone()
        if not campanha:
            raise SystemExit(f"Campanha {a.campanha} nao existe")
        saida = a.saida or f"relatorio_campanha_{a.campanha}_{datetime.now():%Y%m%d_%H%M}.csv"
        if campanha["origem"] == "lista":
            n = relatorio_lista(cur, campanha, saida)
        else:
            n = relatorio_base(cur, campanha, saida)
    print(f"Campanha {a.campanha} ('{campanha['nome']}'): {n} linhas -> {saida}")


if __name__ == "__main__":
    main()
