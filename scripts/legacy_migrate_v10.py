"""Atomic five-table v9 -> v10 upgrade, backed by an immutable SQLite snapshot."""
from __future__ import annotations
import argparse
import sqlite3
from datetime import datetime,timezone
from pathlib import Path


def upgrade_v9(conn:sqlite3.Connection,path:Path):
    from .database import SCHEMA,backup_database
    from news_pipeline.store import RELATION_TABLE
    # Fully copied online backup precedes all DDL and is never overwritten.
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target=path.with_name(f'{path.stem}.pre-v10-{stamp}.sqlite3')
    backup_database(conn,target)
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'companies','forms','items'}.issubset(tables):
        raise ValueError('Migration requires all three existing SEC tables')
    # Rebuild with no implicit commits while preserving rowids as new PK values.
    # Table references intentionally use *_v10 and are rewritten by RENAME TO.
    definitions=[]
    for piece in SCHEMA.split(';'):
        piece=piece.strip()
        if not piece.startswith('CREATE TABLE'):continue
        for name in ('companies','forms','items'):
            piece=piece.replace(f'IF NOT EXISTS {name} (',f'{name}_v10 (')
        piece=piece.replace('REFERENCES companies(id)','REFERENCES companies_v10(id)')
        piece=piece.replace('REFERENCES forms(id)','REFERENCES forms_v10(id)')
        definitions.append(piece)
    rel=RELATION_TABLE.replace('IF NOT EXISTS news_company_relations (','news_company_relations_v10 (')
    rel=rel.replace('company_id INTEGER REFERENCES companies(id)',
                    'company_id INTEGER REFERENCES companies_v10(id)')
    was_foreign_keys=conn.execute('PRAGMA foreign_keys').fetchone()[0]
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        for statement in definitions:conn.execute(statement)
        conn.execute('''INSERT INTO companies_v10
          (id,cik,name,ticker,exchange,created_at,updated_at)
          SELECT rowid,cik,name,ticker,exchange,created_at,updated_at FROM companies''')
        conn.execute('''INSERT INTO forms_v10
          (id,company_id,form_type,form_name,filed_date,index_url,primary_url,
           file_guid,file_name,status,error,created_at,updated_at)
          SELECT f.rowid,c.rowid,f.form_type,f.form_name,
           COALESCE(NULLIF(f.filing_ts_utc,''),f.filed_date || 'T00:00:00Z'),
           f.index_url,f.primary_url,f.file_guid,f.file_name,f.status,f.error,f.created_at,f.updated_at
          FROM forms f JOIN companies c ON c.cik=f.cik''')
        # ROW_NUMBER protects repeated Item numbers across Parts. Save Part text
        # inside the display name rather than silently deleting that context.
        conn.execute('''INSERT INTO items_v10
          (id,form_id,file_guid,file_name,item_type,item_occurrence,item_name,item_text,
           status,ai_impact,ai_confidence,ai_reason,ai_evidence,ai_monetary_effect,
           ai_monetary_effect_reason,price_change_1w_pct,price_change_reason,errors,created_at,updated_at)
          SELECT i.rowid,f.rowid,i.file_guid,i.file_name,i.item_type,
           ROW_NUMBER() OVER (PARTITION BY i.accession,i.item_type ORDER BY i.part,i.rowid),
           CASE WHEN i.part IS NOT NULL AND trim(i.part)!='' THEN i.part || ' · ' || i.item_name
                ELSE i.item_name END,
           i.item_text,i.status,i.ai_impact,i.ai_confidence,i.ai_reason,i.ai_evidence,
           i.ai_monetary_effect,i.ai_monetary_effect_reason,i.price_change_1w_pct,
           i.price_change_reason,i.errors,i.created_at,i.updated_at
          FROM items i JOIN forms f ON f.accession=i.accession''')
        if 'news_company_relations' in tables:
            conn.execute(rel)
            conn.execute('''INSERT INTO news_company_relations_v10
              (relation_id,news_id,company_id,entity_key,entity_type,entity_name,status,
               ai_impact,ai_confidence,ai_reason,ai_evidence,ai_monetary_effect,
               ai_monetary_effect_reason,price_change_1w_pct,price_change_reason,
               errors,created_at,updated_at,match_hits,match_evidence)
              SELECT r.relation_id,r.news_id,c.rowid,r.entity_key,r.entity_type,r.entity_name,r.status,
               r.ai_impact,r.ai_confidence,r.ai_reason,r.ai_evidence,r.ai_monetary_effect,
               r.ai_monetary_effect_reason,r.price_change_1w_pct,r.price_change_reason,
               r.errors,r.created_at,r.updated_at,r.match_hits,r.match_evidence
              FROM news_company_relations r LEFT JOIN companies c ON c.cik=r.company_cik''')
            conn.execute('DROP TABLE news_company_relations')
        for old in ('items','forms','companies'):
            conn.execute(f'DROP TABLE {old}')
        for name in ('companies','forms','items'):
            conn.execute(f'ALTER TABLE {name}_v10 RENAME TO {name}')
        if 'news_company_relations' in tables:
            conn.execute('ALTER TABLE news_company_relations_v10 RENAME TO news_company_relations')
        for statement in SCHEMA.split(';'):
            if statement.strip().startswith('CREATE INDEX'):
                conn.execute(statement.strip())
        if 'news_company_relations' in tables:
            conn.execute('CREATE INDEX idx_news_rel_company ON news_company_relations(company_id,news_id)')
            conn.execute('CREATE INDEX idx_news_rel_entity ON news_company_relations(entity_key,news_id)')
            conn.execute('CREATE INDEX idx_news_rel_analysis ON news_company_relations(status,news_id)')
        checks=conn.execute('PRAGMA foreign_key_check').fetchall()
        if checks:raise RuntimeError(f'Foreign-key errors: {checks[:3]}')
        conn.commit()
    except BaseException:
        conn.rollback();raise
    finally:
        conn.execute(f'PRAGMA foreign_keys={int(bool(was_foreign_keys))}')
    print(f'v10 schema migration completed. Untouched backup: {target}',flush=True)


def main():
    p=argparse.ArgumentParser(description='Upgrade v9 SEC database to v10; auto-backup')
    p.add_argument('--db',type=Path,required=True)
    args=p.parse_args()
    from .database import connect
    with connect(args.db) as conn:
        for name in ('companies','forms','items','news','news_company_relations'):
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone():
                print(name,conn.execute('SELECT COUNT(*) FROM '+name).fetchone()[0])
        print('integrity_check',conn.execute('PRAGMA integrity_check').fetchone()[0])
        print('foreign_key_check',len(conn.execute('PRAGMA foreign_key_check').fetchall()))

if __name__=='__main__':main()
