"""Cria ou atualiza um usuario do painel."""
import argparse, getpass
import psycopg
from argon2 import PasswordHasher

PERFIS = ["admin", "supervisor", "monitoria", "operador", "cliente"]

p = argparse.ArgumentParser(description="Cria ou atualiza um usuario do painel")
p.add_argument("--login", required=True)
p.add_argument("--nome", required=True)
p.add_argument("--perfil", required=True, choices=PERFIS)
p.add_argument("--ramal", help="ramal do operador, ex.: 7001")
a = p.parse_args()

senha = getpass.getpass("Senha (minimo 6 caracteres): ")
if len(senha) < 6:
    raise SystemExit("Senha curta demais.")
if senha != getpass.getpass("Repita a senha: "):
    raise SystemExit("As senhas nao conferem.")

with psycopg.connect("dbname=discadora") as conn:
    conn.execute("""INSERT INTO usuarios (login, nome, senha_hash, perfil, ramal)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (login) DO UPDATE
                    SET nome = EXCLUDED.nome, senha_hash = EXCLUDED.senha_hash,
                        perfil = EXCLUDED.perfil, ramal = EXCLUDED.ramal, ativo = true""",
                 (a.login, a.nome, PasswordHasher().hash(senha), a.perfil, a.ramal))
print(f"Usuario '{a.login}' salvo com perfil '{a.perfil}'.")
