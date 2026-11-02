"""Consultas agregadas: cada fato é calculado separadamente para evitar duplicação."""
from .database import statement


def dashboard_data(conn, ano=None, oficina=None):
    params = {'ano': ano, 'oficina': oficina}
    def query(sql):
        return [dict(r) for r in statement(conn, sql, params).mappings()]
    scope = '(:ano IS NULL OR d.ano=:ano) AND (:oficina IS NULL OR f.sk_oficina=:oficina)'
    # Casts permitem parâmetros nulos também no PostgreSQL.
    scope = scope.replace(':ano IS NULL', 'CAST(:ano AS INTEGER) IS NULL').replace(':oficina IS NULL', 'CAST(:oficina AS INTEGER) IS NULL')
    interests = query(f'''SELECT COUNT(*) AS total FROM dw.fato_interesse f
        JOIN dw.dim_data d ON d.sk_data=f.sk_data_interesse WHERE {scope}''')[0]['total']
    enrollments = query(f'''SELECT COUNT(*) AS total, COUNT(DISTINCT f.sk_participante) AS pessoas,
        COALESCE(SUM(f.valor_contratado),0) AS contratado FROM dw.fato_inscricao f
        JOIN dw.dim_data d ON d.sk_data=f.sk_data_inscricao
        JOIN dw.dim_status s ON s.sk_status=f.sk_status
        WHERE s.dominio='inscricao' AND s.codigo='confirmada' AND {scope}''')[0]
    monthly_scope = scope.replace('d.ano', 'f.ano')
    monthly = query(f'''SELECT f.ano,f.mes,SUM(f.recebido_reais) AS recebido
        FROM bi.v_recebimentos_mensais f WHERE {monthly_scope}
        GROUP BY f.ano,f.mes ORDER BY f.ano,f.mes''')
    classes = query(f'''SELECT t.id_turma,o.nome_oficina,t.data_inicio,f.vagas_ofertadas,
        f.inscritos_confirmados,f.ocupacao_pct FROM bi.v_ocupacao_turma f
        JOIN dw.dim_turma t ON t.sk_turma=f.sk_turma
        JOIN dw.dim_oficina o ON o.sk_oficina=f.sk_oficina
        JOIN dw.dim_data d ON d.sk_data=f.sk_data_inicio WHERE {scope}
        ORDER BY t.data_inicio,t.id_turma''')
    ranking = query(f'''SELECT o.nome_oficina,COUNT(*) AS total FROM dw.fato_interesse f
        JOIN dw.dim_oficina o ON o.sk_oficina=f.sk_oficina
        JOIN dw.dim_data d ON d.sk_data=f.sk_data_interesse WHERE {scope}
        GROUP BY o.sk_oficina,o.nome_oficina ORDER BY total DESC,o.nome_oficina''')
    capacity = sum(r['vagas_ofertadas'] for r in classes)
    confirmed = sum(r['inscritos_confirmados'] for r in classes)
    return dict(interesses=interests, **enrollments, recebido=sum(float(r['recebido']) for r in monthly),
                vagas=capacity, turmas=len(classes), ocupacao=100*confirmed/capacity if capacity else None,
                meses=monthly, ofertas=classes, procura=ranking,
                max_recebido=max((float(r['recebido']) for r in monthly), default=0),
                max_procura=max((r['total'] for r in ranking), default=0))
