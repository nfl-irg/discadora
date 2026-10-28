"""Gera um .sql com os bloqueios novos para rodar no pgAdmin (db_mailing -> d_blocklist)."""
from datetime import datetime
from pathlib import Path
import psycopg


def texto(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


with psycopg.connect("dbname=discadora") as conn:
    novos = conn.execute("""SELECT telefone, motivo, origem, criado_em FROM bloqueios
                            WHERE exportado_em IS NULL ORDER BY criado_em""").fetchall()
    if not novos:
        raise SystemExit("Nenhum bloqueio novo para exportar.")
    destino = Path.home() / "lab" / "exportacoes"
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = destino / f"bloqueios_{datetime.now():%Y%m%d_%H%M%S}.sql"
    linhas = ["-- Bloqueios feitos na tabulacao da discadora. Rodar no pgAdmin, no banco db_mailing.",
              "INSERT INTO d_blocklist (telefone_norm, telefone_orig, motivo, tipo, data_bloqueio) VALUES"]
    valores = [f"  ({texto(t)}, {texto(t)}, {texto(f'{m} ({o})')}, 'numero', '{c:%Y-%m-%d}')"
               for t, m, o, c in novos]
    linhas.append(",\n".join(valores))
    linhas.append("ON CONFLICT (telefone_norm) DO NOTHING;")
    arquivo.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    conn.execute("UPDATE bloqueios SET exportado_em = now() WHERE exportado_em IS NULL")
print(f"{len(novos)} bloqueio(s) exportado(s) -> {arquivo}")
