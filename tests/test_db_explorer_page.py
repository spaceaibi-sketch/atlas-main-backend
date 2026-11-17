"""Ensure the DB page is wired to Dash and never reads rows without authorization."""
import sqlite3
from test_web_layout_smoke import app_module, nodes


def test_db_explorer_is_on_dashboard_and_rejects_unauthorized_queries(monkeypatch,tmp_path):
    m=app_module(monkeypatch,tmp_path)
    components=list(nodes(m.app.layout))
    ids={x.props['id'] for x in components if 'id' in x.props}
    assert {'page-database','nav-database','dbx-table','dbx-schema','dbx-results','dbx-key'} <= ids
    assert m.current_page('/database') == 'database'
    callbacks={func.__name__:func for _,_,func in m.app.callbacks}
    assert {'unlock','show_schema','show_rows','move_page'} <= callbacks.keys()
    db=sqlite3.connect(m.DB_PATH)
    db.execute('CREATE TABLE sample_rows (id INTEGER PRIMARY KEY, value TEXT)')
    db.execute("INSERT INTO sample_rows VALUES (5,'hello')")
    db.commit();db.close()
    assert callbacks['unlock']('/database','guess',None)[0]==[]
    assert callbacks['show_rows']('sample_rows',None,'contains',None,'id','desc',1,0,'guess','/database')[0]==''
    monkeypatch.setenv('ATLAS_DB_EXPLORER_TOKEN','long_secret_for_local_admin')
    options,chosen,status=callbacks['unlock']('/database','long_secret_for_local_admin',None)
    assert 'sample_rows' in [x['value'] for x in options]
    assert 'tables available' in status
    schema,filters,sorts,_,_=callbacks['show_schema']('sample_rows','long_secret_for_local_admin',None,None)
    assert filters and sorts and schema
    result,text,_,next_disabled=callbacks['show_rows']('sample_rows',None,'contains',None,'id','desc',1,0,'long_secret_for_local_admin','/database')
    assert next_disabled and '1 records' in text
    assert any(x.kind=='Article' for x in nodes(result))
