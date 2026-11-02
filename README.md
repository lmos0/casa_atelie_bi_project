# Casa Ateliê

## Executar localmente

Requer Python 3.11 ou superior. No terminal, dentro da pasta do projeto:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export AUTH_USERNAME='admin'
export AUTH_PASSWORD='defina-uma-senha-forte'
python app.py
```

Abra <http://127.0.0.1:5000>. O banco SQLite e a chave são criados em `instance/`; preserve essa pasta entre execuções. As variáveis de ambiente precisam ser definidas novamente em cada novo terminal.

## Render para testes

Configure no painel do Render as variáveis `AUTH_USERNAME`, `AUTH_PASSWORD`, `SECRET_KEY`, `ETL_SECRET` e `SESSION_COOKIE_SECURE=true`. Gere valores aleatórios para `SECRET_KEY` e `ETL_SECRET` executando duas vezes `python3 -c 'import secrets; print(secrets.token_hex(32))'`. Mantenha-os estáveis entre deploys, use valores diferentes e não reutilize uma chave que já tenha sido compartilhada.

Use estes comandos no serviço Web:

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `flask --app app init-db && gunicorn --bind 0.0.0.0:$PORT 'app:create_app()'`

Se `DATABASE_URL` não for definida, a aplicação usa SQLite em `instance/`. No Render, esse arquivo é temporário e os dados podem ser perdidos em reinícios ou novos deploys; use apenas para testes descartáveis.
