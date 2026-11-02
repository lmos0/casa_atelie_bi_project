"""ETL de lote: extrai com pandas, valida e carrega em uma transação."""
import hashlib
import hmac
import io
import json
import re
import uuid
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from .database import ROOT, dimension, insert, row, statement

TYPES = ('oferta_turmas', 'interesses', 'inscricoes', 'pagamentos')
COLUMNS = {kind: tuple(pd.read_csv(ROOT / 'output/mock' / f'{kind}_mock.csv', nrows=0).columns) for kind in TYPES}
IDS = dict(zip(TYPES, ('id_turma', 'id_interesse', 'id_inscricao', 'id_pagamento')))
STATUS = {
    'turma': {'realizada', 'planejada', 'cancelada'},
    'interesse': {'registrado'},
    'inscricao': {'confirmada', 'pendente', 'cancelada'},
    'pagamento': {'liquidado', 'pendente', 'cancelado'},
}


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def extract(files):
    require(1 <= len(files) <= 4, 'Envie de um a quatro arquivos por lote.')
    result, names = [], set()
    for upload in files:
        name = Path(upload.filename.replace('\\', '/')).name
        require(name not in names, 'Os nomes dos arquivos no lote devem ser diferentes.')
        names.add(name)
        raw = upload.read()
        try:
            if name.lower().endswith('.csv'):
                frame = pd.read_csv(io.BytesIO(raw), sep=None, engine='python', dtype=str, keep_default_na=False, encoding='utf-8-sig')
            elif name.lower().endswith('.xlsx'):
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    require(sum(x.file_size for x in archive.infolist()) <= 40_000_000, 'Planilha descompactada excede 40 MB.')
                sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, dtype=str, keep_default_na=False)
                require(len(sheets) == 1, 'Use uma única aba por arquivo Excel.')
                frame = next(iter(sheets.values()))
            else:
                raise ValidationError('Formato não aceito. Use CSV UTF-8 ou XLSX.')
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(f'{name}: não foi possível ler o arquivo; confira formato e codificação.') from exc
        frame.columns = [str(c).strip().lower() for c in frame.columns]
        kinds = [kind for kind in TYPES if set(frame.columns) == set(COLUMNS[kind])]
        require(len(kinds) == 1 and frame.columns.is_unique, f'{name}: colunas diferentes dos modelos disponíveis.')
        require(0 < len(frame) <= 10000, f'{name}: envie entre 1 e 10.000 registros.')
        kind = kinds[0]
        for number, source in enumerate(frame.to_dict('records'), 2):
            cleaned = {key: str(value).strip() for key, value in source.items()}
            result.append((kind, name, number, source, cleaned))
    return sorted(result, key=lambda entry: TYPES.index(entry[0]))


def parsed_date(value, field):
    require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)), f'{field}: use AAAA-MM-DD.')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f'{field}: data inválida.') from exc


def money(value, field):
    require(bool(re.fullmatch(r'\d+(?:[.,]\d{1,2})?', value)), f'{field}: use 120.00 ou 120,00, sem separador de milhar.')
    try:
        amount = Decimal(value.replace(',', '.'))
        require(amount < Decimal('10000000000'), f'{field}: valor excede o limite.')
        return format(amount, '.2f')
    except InvalidOperation as exc:
        raise ValidationError(f'{field}: valor inválido.') from exc


def transform(kind, values):
    values = dict(values)
    optional = {'id_interesse', 'data_confirmacao'} if kind == 'inscricoes' else set()
    for key, value in values.items():
        require(value or key in optional, f'{key}: campo obrigatório.')
        limit = (100 if key in {'cidade', 'nome_oficina'} else 20 if key == 'faixa_etaria_evento' else 30 if key.startswith('status_') or key == 'forma_pagamento' else 40)
        require(len(value) <= limit, f'{key}: limite de {limit} caracteres.')
        if key.startswith('data_') and value:
            values[key] = parsed_date(value, key).isoformat()
        if key.startswith('valor_'):
            values[key] = money(value, key)
        if key.startswith('status_'):
            domain = key.removeprefix('status_')
            values[key] = value.lower()
            require(values[key] in STATUS[domain], f'{key}: valor não permitido.')
    if kind == 'oferta_turmas':
        require(values['data_fim'] >= values['data_inicio'], 'Fim da turma anterior ao início.')
        require(values['vagas_ofertadas'].isdigit() and 0 < int(values['vagas_ofertadas']) <= 100000, 'Vagas devem ser um inteiro de 1 a 100.000.')
        values['vagas_ofertadas'] = str(int(values['vagas_ofertadas']))
    if kind == 'inscricoes':
        require(values['status_inscricao'] != 'confirmada' or values['data_confirmacao'], 'Inscrição confirmada exige data de confirmação.')
        require(not values['data_confirmacao'] or values['data_confirmacao'] >= values['data_inscricao'], 'Confirmação anterior à inscrição.')
    return values


def date_key(conn, value):
    if not value:
        return None
    d = date.fromisoformat(value)
    key = int(d.strftime('%Y%m%d'))
    if not row(conn, 'dw.dim_data', sk_data=key):
        insert(conn, 'dw.dim_data', dict(sk_data=key, data=value, ano=d.year, mes=d.month))
    return key


def load_record(conn, kind, x, secret):
    domain = dict(zip(TYPES, ('turma', 'interesse', 'inscricao', 'pagamento')))[kind]
    status = dimension(conn, 'dim_status', 'sk_status', dict(dominio=domain, codigo=x['status_' + domain]))
    if kind == 'oferta_turmas':
        office = row(conn, 'dw.dim_oficina', id_oficina=x['id_oficina'])
        if office:
            require(office['nome_oficina'] == x['nome_oficina'] and office['modalidade'] == x['modalidade'], 'Oficina já cadastrada com nome/modalidade diferente.')
            office = office['sk_oficina']
        else:
            office = dimension(conn, 'dim_oficina', 'sk_oficina', {k: x[k] for k in ('id_oficina', 'nome_oficina', 'modalidade')})
        turma = dimension(conn, 'dim_turma', 'sk_turma', {k: x[k] for k in ('id_turma', 'data_inicio', 'data_fim')})
        insert(conn, 'dw.fato_oferta_turma', dict(sk_turma=turma, sk_oficina=office, sk_data_inicio=date_key(conn, x['data_inicio']), sk_status=status, vagas_ofertadas=int(x['vagas_ofertadas'])))
        return
    office = row(conn, 'dw.dim_oficina', id_oficina=x['id_oficina'])
    require(office, 'Oficina não encontrada. Importe primeiro oferta_turmas.')
    office = office['sk_oficina']
    person = dimension(conn, 'dim_participante', 'sk_participante', dict(chave_pseudonima=hmac.new(secret.encode(), x['id_pessoa_mock'].encode(), hashlib.sha256).hexdigest()))
    values = dict(sk_participante=person, sk_oficina=office, sk_status=status)
    if kind in {'interesses', 'inscricoes'}:
        values['sk_perfil'] = dimension(conn, 'dim_perfil', 'sk_perfil', dict(faixa_etaria=x['faixa_etaria_evento'], cidade=x['cidade']))
    if kind == 'interesses':
        values.update(id_interesse=x['id_interesse'], sk_data_interesse=date_key(conn, x['data_interesse']), sk_origem=dimension(conn, 'dim_origem', 'sk_origem', dict(codigo=x['origem'])))
        insert(conn, 'dw.fato_interesse', values)
        return
    turma = row(conn, 'dw.dim_turma', id_turma=x['id_turma'])
    require(turma, 'Turma não encontrada. Importe primeiro oferta_turmas.')
    values['sk_turma'] = turma['sk_turma']
    offer = row(conn, 'dw.fato_oferta_turma', sk_turma=turma['sk_turma'])
    require(offer and offer['sk_oficina'] == office, 'Turma não pertence à oficina informada.')
    if kind == 'inscricoes':
        if x['id_interesse']:
            interest = row(conn, 'dw.fato_interesse', id_interesse=x['id_interesse'])
            require(interest and interest['sk_participante'] == person and interest['sk_oficina'] == office, 'Interesse inexistente ou de outra pessoa/oficina.')
            require(interest['sk_data_interesse'] <= int(x['data_inscricao'].replace('-', '')), 'Interesse posterior à inscrição.')
        if x['status_inscricao'] == 'confirmada':
            existing = statement(conn, "SELECT 1 FROM dw.fato_inscricao i JOIN dw.dim_status s ON s.sk_status=i.sk_status WHERE i.sk_participante=:p AND i.sk_turma=:t AND s.codigo='confirmada'", dict(p=person, t=turma['sk_turma'])).first()
            require(not existing, 'Pessoa já tem inscrição confirmada nesta turma.')
        values.update(id_inscricao=x['id_inscricao'], id_interesse=x['id_interesse'] or None, sk_data_inscricao=date_key(conn, x['data_inscricao']), sk_data_confirmacao=date_key(conn, x['data_confirmacao']), valor_contratado=x['valor_contratado'])
        insert(conn, 'dw.fato_inscricao', values)
    else:
        registration = row(conn, 'dw.fato_inscricao', id_inscricao=x['id_inscricao'])
        require(registration and all(registration[k] == values[k] for k in ('sk_participante', 'sk_oficina', 'sk_turma')), 'Inscrição inexistente ou pagamento com pessoa/turma/oficina divergente.')
        require(int(x['data_pagamento'].replace('-', '')) >= registration['sk_data_inscricao'], 'Pagamento anterior à inscrição.')
        values.update(id_pagamento=x['id_pagamento'], id_inscricao=x['id_inscricao'], sk_data_pagamento=date_key(conn, x['data_pagamento']), valor_pago=x['valor_pago'], sk_forma_pagamento=dimension(conn, 'dim_forma_pagamento', 'sk_forma_pagamento', dict(codigo=x['forma_pagamento'].lower())))
        insert(conn, 'dw.fato_pagamento', values)


def import_batch(engine, files, secret):
    records = extract(files)
    batch = str(uuid.uuid4())
    with engine.begin() as conn:
        insert(conn, 'staging.etl_lote', dict(id_lote=batch, criado_em=datetime.now(timezone.utc).isoformat(), status='processando', resumo='{}'))
        for kind, filename, number, original, _ in records:
            insert(conn, 'staging.etl_linha', dict(id_lote=batch, arquivo=filename, numero=number, tipo=kind, conteudo=json.dumps(original, ensure_ascii=False)))
    summary = dict(id_lote=batch, lidas=len(records), inseridas=0, ignoradas=0, erros=[])
    try:
        with engine.begin() as conn:
            # Serializa cargas concorrentes no mesmo banco PostgreSQL.
            if conn.dialect.name == 'postgresql':
                statement(conn, 'SELECT pg_advisory_xact_lock(735019)')
            for kind, filename, number, _, source in records:
                try:
                    values = transform(kind, source)
                    key = values[IDS[kind]]
                    digest = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    existing = row(conn, 'staging.etl_registro', tipo=kind, chave=key)
                    if existing:
                        require(existing['hash_conteudo'] == digest, 'ID já importado com conteúdo diferente. Atualizações exigem revisão; nenhum dado foi sobrescrito.')
                        summary['ignoradas'] += 1
                        continue
                    load_record(conn, kind, values, secret)
                    insert(conn, 'staging.etl_registro', dict(tipo=kind, chave=key, hash_conteudo=digest, id_lote=batch))
                    summary['inseridas'] += 1
                except ValidationError as exc:
                    raise ValidationError(f'{filename}, linha {number}: {exc}') from exc
            statement(conn, "UPDATE staging.etl_lote SET status='concluido', resumo=:r WHERE id_lote=:id", dict(r=json.dumps(summary, ensure_ascii=False), id=batch))
    except Exception as exc:
        summary.update(inseridas=0, ignoradas=0, erros=[str(exc) if isinstance(exc, ValidationError) else 'Falha ao gravar no banco. Consulte o log do servidor.'])
        with engine.begin() as conn:
            statement(conn, "UPDATE staging.etl_lote SET status='rejeitado', resumo=:r WHERE id_lote=:id", dict(r=json.dumps(summary, ensure_ascii=False), id=batch))
        if not isinstance(exc, ValidationError):
            raise
    return summary
