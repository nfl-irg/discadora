"""Gera os ramais WebRTC dos operadores e guarda as senhas no banco.
Pode ser rodado de novo: mantem as senhas dos ramais que ja existem."""
import secrets
from pathlib import Path
import psycopg

INICIO, FIM = 7001, 7050

blocos = ["; Ramais dos operadores - gerado por gerar_ramais.py (nao editar a mao)\n"]
with psycopg.connect("dbname=discadora") as conn:
    for n in range(INICIO, FIM + 1):
        ramal = str(n)
        linha = conn.execute("SELECT senha_sip FROM ramais WHERE ramal = %s", (ramal,)).fetchone()
        if linha:
            senha = linha[0]
        else:
            senha = secrets.token_hex(12)
            conn.execute("INSERT INTO ramais (ramal, senha_sip) VALUES (%s, %s)", (ramal, senha))
        blocos.append(f"""
[{ramal}](endpoint-modelo)
webrtc=yes
auth={ramal}
aors={ramal}
callerid="Operador {ramal}" <{ramal}>

[{ramal}](auth-modelo)
username={ramal}
password={senha}

[{ramal}](aor-modelo)
""")

Path("pjsip_operadores.conf").write_text("".join(blocos))
print(f"{FIM - INICIO + 1} ramais em pjsip_operadores.conf")
