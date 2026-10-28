"""Exporta uma campanha de URA: arquivo para o pipeline (formato f_ura) e relatorio completo."""
import argparse, csv
from datetime import datetime
from pathlib import Path
import psycopg
from psycopg.rows import dict_row

# como cada resultado aparece no arquivo do pipeline (mesmos textos do fornecedor)
PIPELINE = {"respondente": "Atendida", "atendida": "Atendida", "caixa_postal": "MACHINE",
            "nao_atende": "Não Atendida", "recusou": "Não Atendida", "ocupado": "Ocupado"}
# como cada resultado aparece no relatorio completo
RELATORIO = {"respondente": "Respondente", "atendida": "Atendida", "caixa_postal": "Caixa Postal",
             "nao_atende": "Não Atendida", "recusou": "Recusou", "ocupado": "Ocupado",
             "invalido": "Número inválido", "indisponivel": "Desligado/fora de área",
             "falha_rede": "Falha de rede", "erro_originacao": "Erro técnico",
             "abandono": "Abandono", "outro": "Falha"}
ATENDEU = {"respondente", "atendida"}


def data(dt):
    return dt.strftime("%d/%m/%Y %H:%M") if dt else ""


def exportar(campanha, todos=False):
    """Gera os dois arquivos e devolve (arquivo_pipeline, arquivo_relatorio)."""
    a = argparse.Namespace(campanha=campanha, todos=todos)

    with psycopg.connect("dbname=discadora", row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM campanhas WHERE id = %s", (a.campanha,))
        camp = cur.fetchone()
        if not camp or camp["tipo"] != "ura":
            raise SystemExit(f"Campanha {a.campanha} nao existe ou nao e de URA")
        cur.execute("SELECT count(*) AS n FROM ura_perguntas WHERE campanha_id = %s", (a.campanha,))
        n_perguntas = cur.fetchone()["n"]

        cur.execute("""SELECT cc.id, cc.telefone, cc.uf, cc.cidade, cc.dados, cc.status
                       FROM campanha_contatos cc
                       WHERE cc.campanha_id = %s AND (%s OR cc.status = 'finalizado')
                       ORDER BY cc.id""", (a.campanha, a.todos))
        contatos = cur.fetchall()

        cur.execute("""SELECT campanha_contato_id, inicio, resultado, causa_codigo, causa_texto,
                              sip_codigo, conversa_s, amd_status
                       FROM tentativas
                       WHERE campanha_id = %s AND fim IS NOT NULL
                       ORDER BY inicio""", (a.campanha,))
        tentativas = {}
        for t in cur.fetchall():
            tentativas.setdefault(t["campanha_contato_id"], []).append(t)

        cur.execute("""SELECT t.campanha_contato_id, r.ordem, r.tecla
                       FROM ura_respostas r JOIN tentativas t ON t.id = r.tentativa_id
                       WHERE t.campanha_id = %s
                       ORDER BY r.respondida_em""", (a.campanha,))
        respostas = {}
        for r in cur.fetchall():
            respostas.setdefault(r["campanha_contato_id"], {})[r["ordem"]] = r["tecla"]

    destino = Path.home() / "lab" / "exportacoes"
    destino.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    arq_pipeline = destino / f"ura_pipeline_c{a.campanha}_{carimbo}.csv"
    arq_relatorio = destino / f"ura_relatorio_c{a.campanha}_{carimbo}.csv"
    max_tent = max((len(tentativas.get(c["id"], [])) for c in contatos), default=0)

    exportados = respondentes = 0
    with open(arq_pipeline, "w", newline="", encoding="utf-8") as fp, \
         open(arq_relatorio, "w", newline="", encoding="utf-8-sig") as fr:
        wp = csv.writer(fp, delimiter=";")
        wr = csv.writer(fr, delimiter=";")

        wp.writerow(["ID", "ID CAMPANHA", "NOME CAMPANHA", "NOME CLIENTE", "CIDADE", "UF", "CEP",
                     "BAIRRO", "REGIAO", "CLIENTE", "DATA1", "DATA2", "DATA3", "TEL1"]
                    + [f"RESPOSTA{i}" for i in range(1, 13)]
                    + ["STATUS CHAMADA 1", "STATUS CHAMADA 2", "STATUS CHAMADA 3",
                       "STATUS CAMPANHA"])
        cab = ["telefone", "cidade", "uf", "bairro", "regional",
               "resultado", "respondeu", "tentativas"]
        cab += [f"resposta{i}" for i in range(1, n_perguntas + 1)]
        for i in range(1, max_tent + 1):
            cab += [f"data_{i}", f"status_{i}", f"sip_{i}", f"causa_{i}",
                    f"conversa_s_{i}", f"deteccao_{i}"]
        wr.writerow(cab)

        for c in contatos:
            ts = tentativas.get(c["id"], [])
            if not ts:
                continue
            d = c["dados"] or {}
            resp = respostas.get(c["id"], {})
            atendeu = any(t["resultado"] in ATENDEU for t in ts)
            # o formato tem 3 tentativas: se houve atendimento, a janela termina nele
            idx_at = max((i for i, t in enumerate(ts) if t['resultado'] in ATENDEU), default=None)
            janela = ts[max(0, idx_at - 2):idx_at + 1] if idx_at is not None else ts[-3:]
            ultimas = janela + [None] * (3 - len(janela))
            if atendeu:
                st_campanha = "success"
            elif c["status"] == "finalizado":
                st_campanha = "finished"
            else:
                st_campanha = "pending"

            # ---- arquivo do pipeline (30 colunas, igual ao fornecedor) ----
            wp.writerow([f"u{c['id']}", 100000 + a.campanha, camp["nome"], camp["nome"],
                         c["cidade"] or "", c["uf"] or "", "", d.get("bairro", ""),
                         d.get("mesorregiao", ""), d.get("regional", "")]
                        + [data(t["inicio"]) if t else "" for t in ultimas]
                        + [c["telefone"]]
                        + [resp.get(i, "") for i in range(1, 13)]
                        + [PIPELINE.get(t["resultado"], "Falha") if t else "" for t in ultimas]
                        + [st_campanha])

            # ---- relatorio completo ----
            if resp:
                final = "Respondente"
            elif atendeu:
                final = "Atendida"
            else:
                final = RELATORIO.get(ts[-1]["resultado"], ts[-1]["resultado"])
            linha = [c["telefone"], c["cidade"] or "", c["uf"] or "", d.get("bairro", ""),
                     d.get("regional", ""), final, "Sim" if resp else "Não", len(ts)]
            linha += [resp.get(i, "") for i in range(1, n_perguntas + 1)]
            for t in ts:
                causa = (f"{t['causa_codigo']} {t['causa_texto'] or ''}".strip()
                         if t["causa_codigo"] is not None else (t["causa_texto"] or ""))
                linha += [data(t["inicio"]), RELATORIO.get(t["resultado"], t["resultado"]),
                          t["sip_codigo"] or "", causa,
                          "" if t["conversa_s"] is None else t["conversa_s"],
                          t["amd_status"] or ""]
            wr.writerow(linha)
            exportados += 1
            respondentes += bool(resp)

    print(f"Campanha {a.campanha} ('{camp['nome']}'): {exportados} contatos, "
          f"{respondentes} respondentes")
    print(f"  pipeline : {arq_pipeline}")
    print(f"  relatorio: {arq_relatorio}")
    return arq_pipeline, arq_relatorio


def main():
    p = argparse.ArgumentParser(description="Exporta resultados de uma campanha de URA")
    p.add_argument("--campanha", type=int, required=True)
    p.add_argument("--todos", action="store_true",
                   help="inclui contatos em andamento (padrao: so finalizados)")
    a = p.parse_args()
    exportar(a.campanha, a.todos)


if __name__ == "__main__":
    main()
