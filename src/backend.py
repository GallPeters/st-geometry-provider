# -*- coding: utf-8 -*-
"""
Database access.

Two interchangeable backends:
  * python-oracledb (thin, or thick if thin is refused by the server) with a
    session pool and LOBs fetched inline as bytes/str (no per-row LOB round trip).
  * Qt QOCISPATIAL / QOCI driver (the one QGIS itself uses) as a fallback.

Both expose: backend.acquire() -> Conn ; Conn.query(sql, params) -> Rows ;
Conn.fetchall / fetchone / fetch_dicts ; Conn.close().
Bind variables use Oracle named style (:name) in both backends.
"""

import os
import queue
import sys
import threading
import uuid

from qgis.core import (QgsDataSourceUri, QgsProviderRegistry, QgsApplication,
                       QgsAuthMethodConfig, QgsSettings)
from qgis.PyQt.QtCore import QThread, QCoreApplication, QByteArray

from .compat import log, qenum

try:
    import oracledb  # noqa
except Exception:  # pragma: no cover
    oracledb = None

try:
    from qgis.PyQt.QtCore import QVariant as _QVariant
except Exception:  # PyQt6 without QVariant
    _QVariant = None

SETTINGS_BACKEND = "stgeometry/backend"   # auto | oracledb | qsql
SETTINGS_PARALLEL = "stgeometry/parallel"  # sessions per big geometry read (1 = off)
ARRAYSIZE = 2000
SDU = 65536            # requested network packet size (negotiated down by the server if needed)
POOL_MAX = 48
_parallel = None

_lock = threading.RLock()
_backends = {}
_creds = {}
_thick_tried = False


# ----------------------------------------------------------------- settings
def backend_pref():
    try:
        v = QgsSettings().value(SETTINGS_BACKEND, "auto")
    except Exception:
        v = "auto"
    v = str(v or "auto").lower()
    return v if v in ("auto", "oracledb", "qsql") else "auto"


def set_backend_pref(value):
    QgsSettings().setValue(SETTINGS_BACKEND, value)
    with _lock:
        _backends.clear()


SETTINGS_TIMING = "stgeometry/timing"
_timing = None


def index_max_rows():
    """Above this estimated number of rows in view, use a plain scan instead of the ST grid index."""
    try:
        return max(1000, int(QgsSettings().value("stgeometry/index_max_rows", 50000)))
    except Exception:
        return 50000


def overview_enabled():
    """Map rendering only: draw sub-pixel features as one pixel square per style value (no geometry fetch)."""
    try:
        return str(QgsSettings().value("stgeometry/overview", "0")) in ("1", "true", "True")
    except Exception:
        return False


def set_overview(on):
    QgsSettings().setValue("stgeometry/overview", "1" if on else "0")


def overview_pixels():
    """Features whose envelope is smaller than this many screen pixels are drawn as merged pixel cells."""
    try:
        return max(0.1, min(50.0, float(QgsSettings().value("stgeometry/overview_px", 1.0))))
    except Exception:
        return 1.0


def set_overview_pixels(v):
    QgsSettings().setValue("stgeometry/overview_px", float(v))


def curves_mode():
    """off = ArcGIS densified shape | curves = true curves, QGIS segments them | segmentize = plugin segments them."""
    v = str(QgsSettings().value("stgeometry/curves", "off"))
    return v if v in ("off", "curves", "segmentize") else "off"


def set_curves_mode(v):
    QgsSettings().setValue("stgeometry/curves", v)


def curve_tolerance():
    """(type 'angle'|'distance', value): max angle in degrees, or max distance from the arc in metres."""
    t = str(QgsSettings().value("stgeometry/curve_tol_type", "angle"))
    try:
        v = float(QgsSettings().value("stgeometry/curve_tol", 1.0 if t == "angle" else 0.05))
    except Exception:
        v = 1.0 if t == "angle" else 0.05
    return (t if t in ("angle", "distance") else "angle"), max(v, 1e-6)


def set_curve_tolerance(t, v):
    QgsSettings().setValue("stgeometry/curve_tol_type", t)
    QgsSettings().setValue("stgeometry/curve_tol", float(v))


def decoder_pref():
    """auto = local decoder where verified, server = always sde.st_asbinary."""
    try:
        v = str(QgsSettings().value("stgeometry/decoder", "auto"))
    except Exception:
        v = "auto"
    return v if v in ("auto", "server") else "auto"


def set_decoder_pref(v):
    QgsSettings().setValue("stgeometry/decoder", v)


def set_index_max_rows(n):
    QgsSettings().setValue("stgeometry/index_max_rows", max(1000, int(n)))


# ----------------------------------------------------------------- config file
CONFIG_ENV = "ST_GEOMETRY_CONFIG"
CONFIG_NAME = "connections.json"


def config_path():
    """First existing of: env ST_GEOMETRY_CONFIG, <plugin folder>/connections.json,
    <QGIS profile folder>/st_geometry_connections.json (survives plugin re-installs)."""
    cands = []
    if os.environ.get(CONFIG_ENV):
        cands.append(os.environ[CONFIG_ENV])
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(os.path.dirname(here), CONFIG_NAME))     # plugin root folder
    cands.append(os.path.join(here, CONFIG_NAME))
    try:
        cands.append(os.path.join(QgsApplication.qgisSettingsDirPath(), "st_geometry_connections.json"))
    except Exception:
        pass
    for p in cands:
        if os.path.isfile(p):
            return p
    return cands[0]


def load_config():
    """Connections defined in connections.json (plugin folder, or path in env ST_GEOMETRY_CONFIG)."""
    import json
    p = config_path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        log("Cannot read %s: %s" % (p, e), "critical")
        return {}
    out = {}
    for c in data.get("connections", []):
        name = str(c.get("name") or "").strip()
        if name:
            out[name] = c
    return out


def config_connection_names():
    return sorted(load_config().keys())


def timing_enabled():
    global _timing
    if _timing is None:
        try:
            _timing = str(QgsSettings().value(SETTINGS_TIMING, "0")) in ("1", "true", "True")
        except Exception:
            _timing = False
    return _timing


def set_timing(on):
    global _timing
    _timing = bool(on)
    QgsSettings().setValue(SETTINGS_TIMING, "1" if _timing else "0")


def parallel_degree():
    global _parallel
    if _parallel is None:
        try:
            _parallel = int(QgsSettings().value("stgeometry/parallel_v12", 1))
        except Exception:
            _parallel = 1
        _parallel = max(1, min(16, _parallel))
    return _parallel


def set_parallel_degree(n):
    global _parallel
    _parallel = max(1, min(16, int(n)))
    QgsSettings().setValue("stgeometry/parallel_v12", _parallel)


def is_main_thread():
    app = QCoreApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def oracle_connection_names():
    md = QgsProviderRegistry.instance().providerMetadata("oracle")
    if md is None:
        return []
    try:
        return sorted(md.connections().keys())
    except Exception as e:
        log("Cannot list QGIS Oracle connections: %s" % e, "warning")
        return []


# ----------------------------------------------------------------- credentials
class ConnInfo(object):
    def __init__(self, host, port, database, user, password, label):
        self.host = host or ""
        self.port = port or ""
        self.database = database or ""
        self.user = user or ""
        self.password = password or ""
        self.label = label

    @property
    def dsn(self):
        if self.host:
            return "%s:%s/%s" % (self.host, self.port or "1521", self.database)
        return self.database  # TNS alias / full descriptor

    @property
    def key(self):
        return (self.dsn, self.user.upper())


def _prompt(title, label, password=False):
    from qgis.PyQt.QtWidgets import QInputDialog, QLineEdit
    mode = qenum(QLineEdit, "EchoMode", "Password" if password else "Normal")
    text, ok = QInputDialog.getText(None, title, label, mode)
    return text if ok else ""


def conninfo_from_uri(u):
    """Resolve connection parameters from a layer URI (stconn=<QGIS Oracle connection name> or explicit host/dbname/user)."""
    cfgname = u.param("stcfg") if u.hasParam("stcfg") else ""
    if cfgname:
        c = load_config().get(cfgname)
        if c is None:
            raise RuntimeError("Connection '%s' not found in %s" % (cfgname, config_path()))
        host, port = str(c.get("host") or ""), str(c.get("port") or "1521")
        database = str(c.get("service_name") or c.get("database") or c.get("sid") or "")
        if c.get("dsn"):
            host, database = "", str(c["dsn"])
        user, pwd = str(c.get("username") or ""), str(c.get("password") or "")
        ck = "cfg:" + cfgname
        with _lock:
            cached = _creds.get(ck)
        if (not user or not pwd) and cached:
            user, pwd = cached
        if not user or not pwd:
            if not is_main_thread():
                raise RuntimeError("No user/password for configured connection '%s'." % cfgname)
            if not user:
                user = _prompt("ST Geometry Provider - Oracle", "User name for '%s':" % cfgname)
            if user and not pwd:
                pwd = _prompt("ST Geometry Provider - Oracle", "Password for %s@%s:" % (user, cfgname), True)
            if not user or not pwd:
                raise RuntimeError("No credentials given for '%s'." % cfgname)
        with _lock:
            _creds[ck] = (user, pwd)
        return ConnInfo(host, port, database, user, pwd, cfgname)
    name = u.param("stconn") if u.hasParam("stconn") else ""
    if name:
        md = QgsProviderRegistry.instance().providerMetadata("oracle")
        conns = md.connections() if md else {}
        if name not in conns:
            raise RuntimeError("QGIS Oracle connection '%s' not found (Browser > Oracle)." % name)
        cu = QgsDataSourceUri(conns[name].uri())
    else:
        cu = u
    host, port, database = cu.host(), cu.port(), cu.database()
    user, pwd = cu.username(), cu.password()
    authcfg = cu.authConfigId()
    if authcfg:
        cfg = QgsAuthMethodConfig()
        QgsApplication.authManager().loadAuthenticationConfig(authcfg, cfg, True)
        user = cfg.config("username") or user
        pwd = cfg.config("password") or pwd
    ck = name or "%s|%s|%s" % (host, database, user)
    with _lock:
        cached = _creds.get(ck)
    if (not user or not pwd) and cached:
        user, pwd = cached
    if not user or not pwd:
        if not is_main_thread():
            raise RuntimeError("No saved user/password for Oracle connection '%s'." % (name or database))
        if not user:
            user = _prompt("ST Geometry Provider - Oracle", "User name for '%s':" % (name or database))
        if user and not pwd:
            pwd = _prompt("ST Geometry Provider - Oracle", "Password for %s@%s:" % (user, name or database), True)
        if not user or not pwd:
            raise RuntimeError("No credentials given for Oracle connection '%s'." % (name or database))
    with _lock:
        _creds[ck] = (user, pwd)
    return ConnInfo(host, port, database, user, pwd, name or database)


# ----------------------------------------------------------------- oracledb
def _oracledb_version():
    try:
        return tuple(int(p) for p in oracledb.__version__.split(".")[:2])
    except Exception:
        return (0, 0)


def _make_output_type_handler():
    BLOB, CLOB, NCLOB = oracledb.DB_TYPE_BLOB, oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB

    def conv(cursor, type_code):
        if type_code == BLOB:
            return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
        if type_code == CLOB:
            return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
        if type_code == NCLOB:
            return cursor.var(oracledb.DB_TYPE_LONG_NVARCHAR, arraysize=cursor.arraysize)
        return None

    if _oracledb_version() >= (1, 4):
        def handler(cursor, metadata):
            return conv(cursor, metadata.type_code)
    else:
        def handler(cursor, name, default_type, size, precision, scale):
            return conv(cursor, default_type)
    return handler


def _try_thick():
    """Enable python-oracledb thick mode using the Oracle client QGIS ships with (oci.dll)."""
    global _thick_tried
    if not oracledb.is_thin_mode():
        return True
    if _thick_tried:
        return False
    _thick_tried = True
    candidates = [None]
    for d in (os.path.dirname(sys.executable), os.path.join(QgsApplication.prefixPath(), "bin"),
              os.path.join(os.path.dirname(QgsApplication.prefixPath()), "bin"), os.environ.get("ORACLE_HOME")):
        if d and os.path.isdir(d) and d not in candidates:
            candidates.append(d)
    last = None
    for d in candidates:
        try:
            if d:
                oracledb.init_oracle_client(lib_dir=d)
            else:
                oracledb.init_oracle_client()
            log("python-oracledb thick mode enabled (lib_dir=%s)" % d)
            return True
        except Exception as e:
            last = e
    log("python-oracledb thick mode not available: %s" % last, "warning")
    return False


class _OraRows(object):
    def __init__(self, conn, sql, params, arraysize):
        cur = conn.cursor()
        cur.arraysize = arraysize
        try:
            cur.prefetchrows = arraysize + 1
        except Exception:
            pass
        cur.execute(sql, params or {})
        self.cur = cur
        self.names = [d[0].upper() for d in (cur.description or [])]
        self._buf = []
        self._i = 0
        self._done = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= len(self._buf):
            if self._done:
                raise StopIteration
            self._buf = self.cur.fetchmany()
            self._i = 0
            if not self._buf:
                self._done = True
                self.close()
                raise StopIteration
        r = self._buf[self._i]
        self._i += 1
        return r

    next = __next__

    def fetch_batch(self):
        """Next batch of rows as a list ([] when finished)."""
        if self.cur is None:
            return []
        if self._i < len(self._buf):
            b, self._buf, self._i = self._buf[self._i:], [], 0
            return b
        b = self.cur.fetchmany()
        if not b:
            self.close()
        return b

    def close(self):
        c, self.cur = getattr(self, "cur", None), None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


class _ConnBase(object):
    def fetchall(self, sql, params=None, maxrows=None):
        rows = self.query(sql, params)
        out = []
        try:
            for r in rows:
                out.append(tuple(r))
                if maxrows and len(out) >= maxrows:
                    break
        finally:
            rows.close()
        return out

    def fetchone(self, sql, params=None):
        r = self.fetchall(sql, params, 1)
        return r[0] if r else None

    def fetch_dicts(self, sql, params=None, maxrows=None):
        rows = self.query(sql, params)
        out = []
        try:
            for r in rows:
                out.append(dict(zip(rows.names, r)))
                if maxrows and len(out) >= maxrows:
                    break
        finally:
            rows.close()
        return out


class _OraConn(_ConnBase):
    def __init__(self, conn, pool):
        self.conn = conn
        self.pool = pool
        conn.outputtypehandler = _make_output_type_handler()

    def query(self, sql, params=None, arraysize=ARRAYSIZE):
        return _OraRows(self.conn, sql, params, arraysize)

    def close(self):
        c, self.conn = self.conn, None
        if c is None:
            return
        try:
            if self.pool is not None:
                self.pool.release(c)
            else:
                c.close()
        except Exception:
            try:
                c.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


_DONE = object()


class ParallelRows(object):
    """
    Iterate rows of one or more SQL statements fetched concurrently by background
    threads, each on its own pooled connection. Rows of different partitions are
    interleaved (order is irrelevant for QGIS). Bounded queue = bounded memory.
    close() stops the workers (also when QGIS cancels a render).
    """

    def __init__(self, backend, parts, arraysize=ARRAYSIZE):
        self._q = queue.Queue(maxsize=3 * len(parts) + 2)
        self._stop = threading.Event()
        self._pending = len(parts)
        self._buf = []
        self._i = 0
        self._threads = []
        for sql, params in parts:
            th = threading.Thread(target=self._worker, args=(backend, sql, params, arraysize),
                                  name="stgeometry-fetch", daemon=True)
            th.start()
            self._threads.append(th)

    def _put(self, item):
        waited = 0.0
        while not self._stop.is_set():
            try:
                self._q.put(item, timeout=0.25)
                return True
            except queue.Full:
                waited += 0.25
                if waited > 300:      # consumer abandoned us
                    self._stop.set()
        return False

    def _worker(self, backend, sql, params, arraysize):
        conn = rows = None
        try:
            conn = backend.acquire()
            rows = conn.query(sql, params, arraysize)
            while not self._stop.is_set():
                batch = rows.fetch_batch()
                if not batch:
                    break
                if not self._put(batch):
                    break
        except Exception as e:
            self._put(e)
        finally:
            if rows is not None:
                try:
                    rows.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._put(_DONE)

    def __iter__(self):
        return self

    def __next__(self):
        while self._i >= len(self._buf):
            if self._pending <= 0:
                raise StopIteration
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                if not any(t.is_alive() for t in self._threads) and self._q.empty():
                    self._pending = 0
                continue
            if item is _DONE:
                self._pending -= 1
                continue
            if isinstance(item, Exception):
                self.close()
                raise item
            self._buf, self._i = item, 0
        r = self._buf[self._i]
        self._i += 1
        return r

    next = __next__

    def close(self):
        self._stop.set()
        self._pending = 0
        self._buf = []
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def __del__(self):
        try:
            self._stop.set()
        except Exception:
            pass


class OracledbBackend(object):
    threaded = True

    def __init__(self, ci):
        self.ci = ci
        self.pool = None
        # 1) plain connection first: a failed connect() does not lock thin mode,
        #    so thick mode can still be enabled afterwards (create_pool would lock it).
        self.extra = {"sdu": SDU}
        try:
            test = oracledb.connect(user=ci.user, password=ci.password, dsn=ci.dsn, **self.extra)
        except TypeError:
            self.extra = {}
            test = oracledb.connect(user=ci.user, password=ci.password, dsn=ci.dsn)
        except Exception as e:
            if "sdu" not in str(e).lower():
                raise
            self.extra = {}
            test = oracledb.connect(user=ci.user, password=ci.password, dsn=ci.dsn)
        try:
            cur = test.cursor()
            cur.execute("SELECT 1 FROM dual")
            cur.fetchall()
            cur.close()
        finally:
            test.close()
        # 2) pool
        getmode = getattr(oracledb, "POOL_GETMODE_NOWAIT", None)
        kw = dict(user=ci.user, password=ci.password, dsn=ci.dsn, min=0, max=POOL_MAX, increment=1)
        kw.update(self.extra)
        if getmode is not None:
            kw["getmode"] = getmode
        self.pool = oracledb.create_pool(**kw)
        try:
            c = self.acquire()
            try:
                c.fetchone("SELECT 1 FROM dual")
            finally:
                c.close()
        except Exception:
            try:
                self.pool.close(force=True)
            except Exception:
                pass
            raise
        self.kind = "oracledb-thin" if oracledb.is_thin_mode() else "oracledb-thick"

    def acquire(self):
        try:
            return _OraConn(self.pool.acquire(), self.pool)
        except Exception as e:
            # pool exhausted (NOWAIT) -> standalone connection, never block
            msg = str(e)
            if "DPY-4005" in msg or "ORA-24418" in msg or "exhausted" in msg.lower():
                c = oracledb.connect(user=self.ci.user, password=self.ci.password, dsn=self.ci.dsn, **self.extra)
                return _OraConn(c, None)
            raise


# ----------------------------------------------------------------- Qt OCI
def _qnorm(v):
    if v is None:
        return None
    if isinstance(v, QByteArray):
        return bytes(v.data())
    if _QVariant is not None and isinstance(v, _QVariant):
        return None if v.isNull() else v.value()
    return v


class _QRows(object):
    def __init__(self, db, sql, params):
        from qgis.PyQt.QtSql import QSqlQuery
        q = QSqlQuery(db)
        q.setForwardOnly(True)
        if not q.prepare(sql):
            raise RuntimeError(q.lastError().text())
        for k, v in (params or {}).items():
            q.bindValue(":" + k, v)
        ex = getattr(q, "exec_", None) or getattr(q, "exec")
        if not ex():
            raise RuntimeError(q.lastError().text())
        rec = q.record()
        self.n = rec.count()
        self.names = [rec.fieldName(i).upper() for i in range(self.n)]
        self.q = q

    def __iter__(self):
        return self

    def __next__(self):
        q = self.q
        if q is None or not q.next():
            self.close()
            raise StopIteration
        return tuple(None if q.isNull(i) else _qnorm(q.value(i)) for i in range(self.n))

    next = __next__

    def close(self):
        q, self.q = getattr(self, "q", None), None
        if q is not None:
            try:
                q.finish()
            except Exception:
                pass


class _QConn(_ConnBase):
    def __init__(self, ci):
        from qgis.PyQt.QtSql import QSqlDatabase
        self.name = "stgeom_" + uuid.uuid4().hex
        db = QSqlDatabase.addDatabase("QOCISPATIAL", self.name)
        if not db.isValid():
            QSqlDatabase.removeDatabase(self.name)
            db = QSqlDatabase.addDatabase("QOCI", self.name)
        db.setDatabaseName(ci.dsn)
        db.setUserName(ci.user)
        db.setPassword(ci.password)
        db.setConnectOptions("OCI_ATTR_PREFETCH_ROWS=%d" % ARRAYSIZE)
        if not db.open():
            err = db.lastError().text()
            db = None
            QSqlDatabase.removeDatabase(self.name)
            raise RuntimeError("Qt OCI connection failed: %s" % err)
        self.db = db

    def query(self, sql, params=None, arraysize=ARRAYSIZE):
        return _QRows(self.db, sql, params)

    def close(self):
        db, self.db = getattr(self, "db", None), None
        if db is None:
            return
        try:
            db.close()
        except Exception:
            pass
        del db
        try:
            from qgis.PyQt.QtSql import QSqlDatabase
            QSqlDatabase.removeDatabase(self.name)
        except Exception:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class QSqlBackend(object):
    kind = "qt-oci"
    threaded = False   # Qt SQL connections are bound to their thread

    def __init__(self, ci):
        self.ci = ci
        c = self.acquire()
        c.close()

    def acquire(self):
        return _QConn(self.ci)


# ----------------------------------------------------------------- factory
def _is_auth_error(msg):
    return any(code in msg for code in ("ORA-01017", "ORA-28000", "ORA-01005", "ORA-28001"))


def get_backend(u):
    """Return a (cached) backend for the layer URI."""
    ci = conninfo_from_uri(u)
    pref = backend_pref()
    key = ci.key + (pref,)
    with _lock:
        b = _backends.get(key)
        if b is not None:
            return b
        errors = []
        if pref in ("auto", "oracledb"):
            if oracledb is None:
                errors.append("python-oracledb not installed")
            else:
                try:
                    b = OracledbBackend(ci)
                except Exception as e:
                    msg = str(e)
                    errors.append("oracledb(%s): %s" % ("thin" if oracledb.is_thin_mode() else "thick", msg))
                    if _is_auth_error(msg):
                        with _lock:
                            for k in list(_creds):
                                if _creds[k] == (ci.user, ci.password):
                                    del _creds[k]
                        raise RuntimeError(msg)
                    if oracledb.is_thin_mode() and _try_thick():
                        try:
                            b = OracledbBackend(ci)
                        except Exception as e2:
                            errors.append("oracledb(thick): %s" % e2)
        if b is None and pref in ("auto", "qsql"):
            try:
                b = QSqlBackend(ci)
            except Exception as e:
                errors.append(str(e))
        if b is None:
            raise RuntimeError(" ; ".join(errors) or "no database backend available")
        if errors:
            log("Backend fallback for %s: %s" % (ci.label, " ; ".join(errors)), "warning")
        log("Connected to %s using %s" % (ci.label, b.kind))
        _backends[key] = b
        return b
