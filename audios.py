"""Busca e exporta gravacoes de chamadas atendidas (checagem e monitoria)."""
import argparse, csv, re, shutil, subprocess
from datetime import datetime
from pathlib import Path
import psycopg
from psycopg.rows import dict_row

from importar_lista import normalizar_telefone   # mesma regra do importador

PASTA_GRAVACOES = Path("/var/spool/asterisk/recording")


def ler_telefones(args):
    brutos = list(args.telefone or [])
    if args.arquivo_telefones:
        for linha in Path(args.arquivo_telefones).read_text(encoding="utf-8-sig").splitlines():
            primeira = re.split(r"[;,\t]", linha)[0].strip()
            if primeira:
                brutos.append(primeira)
    validos, invalidos = [], []
    for b in brutos:
        t = normalizar_telefone(b)
        (validos if t else invalidos).append(t or b)
    return validos, invalidos


def main():
    p = argparse.ArgumentParser(description="Busca e exporta gravacoes")
    p.add_argument("--telefone", nargs="+", help="um ou mais telefones, em qualquer formato")
    p.add_argument("--arquivo-telefones", help="arquivo com um telefone por linha")
    p.add_argument("--campanha", type=int)
    p.add_argument("--operador", help="ramal do operador, ex.: 7001")
    p.add_argument("--min-segundos", type=int, default=0, help="duracao minima da conversa")
    p.add_argument("--de", help="data inicial AAAA-MM-DD")
    p.add_argument("--ate", help="data final AAAA-MM-DD (inclusive)")
    p.add_argument("--mp3", action="store_true", help="converte para MP3 (~10x menor)")
    a = p.parse_args()

    telefones, invalidos = ler_telefones(a)
    if invalidos:
        print(f"Atencao: {len(invalidos)} telefone(s) invalido(s) ignorado(s): {invalidos[:10]}")
    if (a.telefone or a.arquivo_telefones) and not telefones:
        raise SystemExit("Nenhum telefone valido informado.")

    with psycopg.connect("dbname=discadora", row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""SELECT t.inicio, t.operador, t.telefone, t.conversa_s, t.canal,
                              c.nome AS campanha
                       FROM tentativas t JOIN campanhas c ON c.id = t.campanha_id
                       WHERE t.atendida_em IS NOT NULL
                         AND (%(tels)s::text[]   IS NULL OR t.telefone = ANY(%(tels)s))
                         AND (%(campanha)s::int  IS NULL OR t.campanha_id = %(campanha)s)
                         AND (%(operador)s::text IS NULL OR t.operador = %(operador)s)
                         AND coalesce(t.conversa_s, 0) >= %(min)s
                         AND (%(de)s::date  IS NULL OR t.inicio >= %(de)s::date)
                         AND (%(ate)s::date IS NULL OR t.inicio < %(ate)s::date + 1)
                       ORDER BY t.telefone, t.inicio""",
                    {"tels": telefones or None, "campanha": a.campanha, "operador": a.operador,
                     "min": a.min_segundos, "de": a.de, "ate": a.ate})
        chamadas = cur.fetchall()

    if not chamadas:
        raise SystemExit("Nenhuma gravacao encontrada com esses filtros.")
    if telefones:
        encontrados = {ch["telefone"] for ch in chamadas}
        sem_gravacao = [t for t in telefones if t not in encontrados]
        if sem_gravacao:
            print(f"Sem chamada atendida para: {sem_gravacao[:20]}")

    destino = Path.home() / "lab" / "exportacoes" / f"audios_{datetime.now():%Y%m%d_%H%M%S_%f}"
    destino.mkdir(parents=True)
    exportadas = sem_arquivo = 0
    with open(destino / "indice.csv", "w", newline="", encoding="utf-8-sig") as arq:
        w = csv.writer(arq, delimiter=";")
        w.writerow(["arquivo", "data_hora", "campanha", "operador", "telefone", "duracao_s"])
        for ch in chamadas:
            origem = PASTA_GRAVACOES / f"{ch['canal']}.wav"
            if not origem.exists():
                sem_arquivo += 1
                continue
            nome = (f"{ch['telefone']}_{ch['inicio']:%Y%m%d_%H%M%S}_"
                    f"op{ch['operador']}_{ch['conversa_s']}s")
            if a.mp3:
                arquivo = nome + ".mp3"
                subprocess.run(["lame", "--quiet", "-b", "32", str(origem),
                                str(destino / arquivo)], check=True)
            else:
                arquivo = nome + ".wav"
                shutil.copy2(origem, destino / arquivo)
            w.writerow([arquivo, f"{ch['inicio']:%d/%m/%Y %H:%M:%S}", ch["campanha"],
                        ch["operador"], ch["telefone"], ch["conversa_s"]])
            exportadas += 1

    zip_path = shutil.make_archive(str(destino), "zip", destino)
    print(f"{exportadas} gravacao(oes) exportada(s) ({sem_arquivo} sem arquivo) -> {zip_path}")


if __name__ == "__main__":
    main()
