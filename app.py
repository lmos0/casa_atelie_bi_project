import json
import os
import secrets
from pathlib import Path

import click
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy.exc import SQLAlchemyError

from casa_atelie.database import ROOT, clear_database, connect, initialize, statement
from casa_atelie.etl import TYPES, ValidationError, import_batch



def create_app(config=None):
    app = Flask(__name__)
    instance = ROOT / 'instance'
    instance.mkdir(exist_ok=True)
    secret_key = os.environ.get('SECRET_KEY')
    if secret_key is None:
        # Segredo persistente local; em hospedagem, defina SECRET_KEY no ambiente.
        secret_path = instance / 'secret.key'
        if not secret_path.exists():
            try:
                fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    handle.write(secrets.token_hex(32))
            except FileExistsError:
                pass
        secret_key = secret_path.read_text().strip()
    else:
        secret_key = secret_key.strip()
    if not secret_key:
        raise RuntimeError('Defina SECRET_KEY com um segredo não vazio.')

    etl_secret = os.environ.get('ETL_SECRET', secret_key).strip()
    if not etl_secret:
        raise RuntimeError('Defina ETL_SECRET com um segredo não vazio.')

    app.config.update(
        SECRET_KEY=secret_key,
        ETL_SECRET=etl_secret,
        DATABASE_URL=os.environ.get('DATABASE_URL', f'sqlite:///{instance / "casa_atelie.db"}'),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '').strip().lower() in {'1', 'true', 'yes', 'on'},
        AUTH_USERNAME=os.environ.get('AUTH_USERNAME'),
        AUTH_PASSWORD=os.environ.get('AUTH_PASSWORD'),
    )
    if config:
        app.config.update(config)
    if not app.config['AUTH_USERNAME'] or not app.config['AUTH_PASSWORD']:
        raise RuntimeError('Defina AUTH_USERNAME e AUTH_PASSWORD antes de iniciar a aplicação.')
    engine = connect(app.config['DATABASE_URL'])
    app.extensions['database'] = engine

    def valid_csrf(token):
        return bool(session.get('csrf')) and secrets.compare_digest(
            token.encode('utf-8'), session['csrf'].encode('utf-8')
        )

    @app.before_request
    def require_login():
        if request.endpoint in ('login', 'static'):
            return
        if session.get('user') != app.config['AUTH_USERNAME']:
            if request.path.startswith('/api/'):
                return jsonify(erro='Faça login para continuar.'), 401
            return redirect(url_for('login'))

    @app.after_request
    def prevent_page_cache(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if session.get('user') == app.config['AUTH_USERNAME']:
            return redirect(url_for('index'))
        session.setdefault('csrf', secrets.token_urlsafe(32))
        error = None
        status = 200
        if request.method == 'POST':
            if not valid_csrf(request.form.get('csrf', '')):
                error, status = 'Sessão expirada. Atualize a página.', 403
            else:
                username_ok = secrets.compare_digest(
                    request.form.get('username', '').encode('utf-8'),
                    app.config['AUTH_USERNAME'].encode('utf-8'),
                )
                password_ok = secrets.compare_digest(
                    request.form.get('password', '').encode('utf-8'),
                    app.config['AUTH_PASSWORD'].encode('utf-8'),
                )
                if username_ok and password_ok:
                    session.clear()
                    session['user'] = app.config['AUTH_USERNAME']
                    session['csrf'] = secrets.token_urlsafe(32)
                    return redirect(url_for('index'))
                error, status = 'Usuário ou senha inválidos.', 401
        return render_template('login.html', erro=error), status

    @app.post('/logout')
    def logout():
        if not valid_csrf(request.form.get('csrf', '')):
            return 'Sessão expirada. Atualize a página.', 403
        session.clear()
        return redirect(url_for('login'))

    @app.cli.command('init-db')
    def init_db():
        initialize(engine)
        click.echo('Schema inicializado. Nenhuma tabela existente foi apagada.')

    @app.get('/')
    def index():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return render_template('index.html', csrf=session['csrf'], database=engine.dialect.name)

    @app.template_filter('brl')
    def brl(value):
        return 'R$ ' + f'{float(value):,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')

    @app.template_filter('percentual')
    def percentage(value):
        return 'Não calculável' if value is None else f'{float(value):.1f}%'.replace('.', ',')

    @app.get('/dados')
    def dashboard():
        from casa_atelie.dashboard import dashboard_data
        try:
            ano = int(request.args['ano']) if request.args.get('ano') else None
            oficina = int(request.args['oficina']) if request.args.get('oficina') else None
            if (ano is not None and not 1 <= ano <= 9999) or (oficina is not None and oficina < 1):
                raise ValueError
        except ValueError:
            return 'Filtro inválido. Use ano e oficina numéricos.', 400
        try:
            with engine.connect() as conn:
                years = statement(conn, 'SELECT DISTINCT ano FROM dw.dim_data ORDER BY ano').scalars().all()
                offices = statement(conn, 'SELECT sk_oficina,nome_oficina FROM dw.dim_oficina ORDER BY nome_oficina').mappings().all()
                data = dashboard_data(conn, ano, oficina)
            return render_template('dados.html', data=data, anos=years, oficinas=offices, ano=ano, oficina=oficina)
        except SQLAlchemyError:
            app.logger.exception('Falha ao consultar painel')
            return render_template('dados.html', data=None, erro='Banco indisponível ou ainda não inicializado. Execute flask --app app init-db.'), 503

    @app.get('/modelos/<kind>')
    def model(kind):
        if kind not in TYPES:
            return jsonify(erro='Modelo inexistente.'), 404
        return send_file(ROOT / 'output/dados_teste' / f'{kind}_teste.csv', as_attachment=True)

    @app.post('/api/importar')
    def upload():
        if not session.get('csrf') or not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            return jsonify(erro='Sessão expirada. Atualize a página.'), 403
        try:
            result = import_batch(engine, request.files.getlist('arquivos'), app.config['ETL_SECRET'])
            return jsonify(result), 422 if result['erros'] else 200
        except ValidationError as exc:
            return jsonify(erro=str(exc)), 422
        except SQLAlchemyError:
            app.logger.exception('Erro de banco durante importação')
            return jsonify(erro='Não foi possível gravar. Confira o banco e execute flask --app app init-db.'), 500
        except Exception:
            app.logger.exception('Erro inesperado durante importação')
            return jsonify(erro='Falha inesperada. Consulte o log do servidor.'), 500

    @app.post('/api/limpar')
    def clear():
        if not valid_csrf(request.headers.get('X-CSRF-Token', '')):
            return jsonify(erro='Sessão expirada. Atualize a página.'), 403
        try:
            clear_database(engine)
            return jsonify(mensagem='Banco limpo. Você já pode importar novos arquivos.')
        except SQLAlchemyError:
            app.logger.exception('Erro ao limpar banco')
            return jsonify(erro='Não foi possível limpar o banco. Nenhuma alteração foi aplicada.'), 500

    @app.get('/api/resumo')
    def summary():
        try:
            with engine.connect() as conn:
                counts = {kind: statement(conn, 'SELECT COUNT(*) FROM dw.' + table).scalar_one() for kind, table in zip(TYPES, ('fato_oferta_turma', 'fato_interesse', 'fato_inscricao', 'fato_pagamento'))}
                batches = statement(conn, 'SELECT id_lote, criado_em, status, resumo FROM staging.etl_lote ORDER BY criado_em DESC LIMIT 10').mappings().all()
                return jsonify(contagens=counts, lotes=[dict(b, resumo=json.loads(b['resumo'])) for b in batches])
        except SQLAlchemyError:
            return jsonify(erro='Banco ainda não inicializado. Execute flask --app app init-db.'), 503

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(erro='O lote excede 10 MB. Envie arquivos menores.'), 413

    return app


if __name__ == '__main__':
    app = create_app()
    initialize(app.extensions['database'])
    host = os.environ.get('HOST', '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1')
    port = int(os.environ.get('PORT', '5000'))
    app.run(host=host, port=port, debug=False)
