from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, g, abort, Response
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from slugify import slugify as make_slug
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.engine import Engine
import qrcode
import io
import re
import os
import secrets
import time
import random
import queue
import threading

# ==================== ЗАГРУЗКА .env ====================
load_dotenv()

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
app = Flask(__name__)

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError('SECRET_KEY не задан. Добавьте его в .env')
app.config['SECRET_KEY'] = SECRET_KEY

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'toy_shop.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_size': 10,
    'max_overflow': 20,
}

# ==================== БЕЗОПАСНОСТЬ COOKIE ====================
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,           # True только с HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    WTF_CSRF_TIME_LIMIT=7200,
    WTF_CSRF_HEADERS=['X-CSRFToken', 'X-CSRF-Token'],
)

# ==================== FLASK-MAIL ====================
# Если MAIL_SERVER не задан — письма не отправляются (только лог).
# Это безопасный режим для локальной разработки.
app.config.update(
    MAIL_SERVER=os.environ.get('MAIL_SERVER', ''),
    MAIL_PORT=int(os.environ.get('MAIL_PORT', '25') or 25),
    MAIL_USE_TLS=os.environ.get('MAIL_USE_TLS', '0') == '1',
    MAIL_USE_SSL=os.environ.get('MAIL_USE_SSL', '0') == '1',
    MAIL_USERNAME=os.environ.get('MAIL_USERNAME', ''),
    MAIL_PASSWORD=os.environ.get('MAIL_PASSWORD', ''),
    MAIL_DEFAULT_SENDER=os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@mir-igrushek.ru'),
    MAIL_SUPPRESS_SEND=not bool(os.environ.get('MAIL_SERVER')),
)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
mail = Mail(app)


# ==================== SQLITE PRAGMA (WAL + кэш) ====================
@event.listens_for(Engine, 'connect')
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Включает WAL-режим и оптимизации для SQLite."""
    module_name = type(dbapi_connection).__module__ or ''
    if 'sqlite' not in module_name.lower():
        return

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA synchronous=NORMAL')
        cursor.execute('PRAGMA cache_size=-64000')   # 64 МБ page cache
        cursor.execute('PRAGMA temp_store=MEMORY')
        cursor.execute('PRAGMA mmap_size=268435456') # 256 МБ mmap
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.execute('PRAGMA busy_timeout=5000')
    except Exception:
        pass
    finally:
        cursor.close()


# ==================== НАСТРОЙКИ ====================
ADMIN_LOGIN = os.environ.get('ADMIN_LOGIN', 'admin')

_admin_plain = os.environ.get('ADMIN_PASSWORD')
if not _admin_plain:
    raise RuntimeError('ADMIN_PASSWORD не задан в .env')

ADMIN_PASSWORD_HASH = generate_password_hash(_admin_plain)

ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip()

SHOP_ADDRESS = 'г. Ижевск, ул. Труда 8'
SHOP_PHONE = '+7 (3412) 45-67-89'

ORDER_STATUSES = ['Новый', 'Оплачен', 'Собирается', 'Отправлен', 'Доставлен', 'Отменён']
CANCELLABLE_STATUSES = ['Новый', 'Оплачен']
EDITABLE_STATUSES = ['Новый']
DELETABLE_STATUSES = ['Отменён', 'Доставлен']

MAX_CART_UNIQUE_ITEMS = 50
MAX_IMAGE_URL_LEN = 500

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$')

PAYMENT_METHODS = {
    'card_on_delivery': 'Оплата картой при получении',
    'cash_on_delivery': 'Оплата наличными при получении',
    'online_demo': 'Онлайн-оплата (демо)',
}

# ==================== НАСТРОЙКИ МОНИТОРИНГА ====================
REQUEST_LOG_KEEP = 500
MONITORING_IGNORE_PREFIXES = (
    '/static/',
    '/favicon.ico',
    '/admin/api/route-metrics',
    '/order/',            # QR-коды и страницы заказов не считаем (шум)
)
METRICS_FLUSH_INTERVAL = 5.0
METRICS_FLUSH_MAX_BATCH = 300

# ==================== НАСТРОЙКИ ГЕНЕРАЦИИ ДАННЫХ ====================
SEED_TOYS_COUNT = 10000
SEED_ORDERS_COUNT = 10000
SEED_ITEMS_PER_ORDER_MIN = 1
SEED_ITEMS_PER_ORDER_MAX = 5
SEED_BATCH_SIZE = 1000

# ==================== НАСТРОЙКИ ПАГИНАЦИИ ====================
PER_PAGE_CATALOG = 24
PER_PAGE_ADMIN_PRODUCTS = 50
PER_PAGE_ADMIN_ORDERS = 50
PER_PAGE_MY_ORDERS = 20

# ==================== КЭШ КАТЕГОРИЙ ====================
CATEGORIES_CACHE_TTL = 300  # 5 минут
_categories_cache = {'data': None, 'expires_at': 0}


def get_categories_cached():
    """Возвращает список категорий с кэшем на 5 минут."""
    now = time.time()
    if _categories_cache['data'] is not None and now < _categories_cache['expires_at']:
        return _categories_cache['data']

    cats = [c[0] for c in
            db.session.query(Toy.category).distinct().order_by(Toy.category).all()]
    _categories_cache['data'] = cats
    _categories_cache['expires_at'] = now + CATEGORIES_CACHE_TTL
    return cats


def invalidate_categories_cache():
    """Сбросить кэш категорий (после CRUD товаров)."""
    _categories_cache['data'] = None
    _categories_cache['expires_at'] = 0


# ==================== МОДЕЛИ ====================
class Toy(db.Model):
    __tablename__ = 'toys'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    price = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(100), nullable=False, index=True)
    age = db.Column(db.String(20), default='3+')
    description = db.Column(db.Text, default='')
    image = db.Column(db.String(500), default='')
    rating = db.Column(db.Float, default=5.0, index=True)

    __table_args__ = (
        db.Index('ix_toys_rating_id', 'rating', 'id'),
    )


class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    full_name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(200), default='')
    address = db.Column(db.Text, nullable=False)
    payment_method = db.Column(db.String(50), nullable=False, default='card_on_delivery')
    total = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(50), default='Новый', index=True)
    cancel_reason = db.Column(db.String(300), default='')

    items = db.relationship('OrderItem', backref='order', cascade='all, delete-orphan')

    __table_args__ = (
        db.Index('ix_orders_status_id', 'status', 'id'),
    )


class OrderItem(db.Model):
    __tablename__ = 'order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    toy_name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    subtotal = db.Column(db.Integer, nullable=False)


class RouteMetric(db.Model):
    __tablename__ = 'route_metrics'

    id = db.Column(db.Integer, primary_key=True)
    endpoint = db.Column(db.String(200), nullable=False, unique=True, index=True)
    method = db.Column(db.String(10), nullable=False)
    path_rule = db.Column(db.String(200), nullable=False)

    count = db.Column(db.Integer, default=0)
    total_ms = db.Column(db.Float, default=0.0)
    min_ms = db.Column(db.Float, default=0.0)
    max_ms = db.Column(db.Float, default=0.0)
    errors = db.Column(db.Integer, default=0)

    last_ms = db.Column(db.Float, default=0.0)
    last_status = db.Column(db.Integer, default=200)
    last_called_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def avg_ms(self):
        return round(self.total_ms / self.count, 2) if self.count else 0.0


class RequestLog(db.Model):
    __tablename__ = 'request_logs'

    id = db.Column(db.Integer, primary_key=True)
    endpoint = db.Column(db.String(200), index=True)
    method = db.Column(db.String(10))
    path = db.Column(db.String(300))
    status = db.Column(db.Integer)
    duration_ms = db.Column(db.Float)
    created_at = db.Column(db.DateTime, index=True)


# ==================== ХЕЛПЕРЫ ====================
def get_toy(toy_id):
    return db.session.get(Toy, toy_id)


def get_order_by_token(token):
    return Order.query.filter_by(token=token).first()


def get_cart():
    return session.get('cart', {})


def save_cart(cart):
    session['cart'] = cart
    session.modified = True


def cart_total_items():
    return sum(get_cart().values())


def is_admin():
    return session.get('is_admin', False)


def is_safe_url(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(target)
    return (
        test_url.scheme in ('http', 'https')
        and ref_url.netloc == test_url.netloc
    )


def is_safe_image_url(url):
    if not url:
        return True
    if len(url) > MAX_IMAGE_URL_LEN:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def remember_order(token):
    tokens = session.get('order_tokens', [])
    if token not in tokens:
        tokens.append(token)
        session['order_tokens'] = tokens[-50:]
    session.modified = True


def my_order_tokens():
    return session.get('order_tokens', [])


def toy_slug(toy):
    """SEO-slug для URL товара. Fallback: toy-<id>."""
    base = make_slug(toy.name or '', max_length=80, lowercase=True)
    return base or f'toy-{toy.id}'


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_admin():
            flash('Требуется вход в админ-панель', 'error')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return wrapper


def _validate_toy_form(form):
    errors = []
    name = form.get('name', '').strip()
    if not name:
        errors.append('Укажите название товара')
    if len(name) > 200:
        errors.append('Название слишком длинное (макс. 200)')

    try:
        price = int(form.get('price', 0))
        if price < 0:
            errors.append('Цена не может быть отрицательной')
    except (TypeError, ValueError):
        errors.append('Некорректная цена')
        price = 0

    try:
        rating = float(form.get('rating', 5.0))
        if not (0 <= rating <= 5):
            errors.append('Рейтинг должен быть от 0 до 5')
    except (TypeError, ValueError):
        errors.append('Некорректный рейтинг')
        rating = 5.0

    category = form.get('category', '').strip()
    if not category:
        errors.append('Укажите категорию')
    if len(category) > 100:
        errors.append('Категория слишком длинная (макс. 100)')

    image = form.get('image', '').strip()
    if not is_safe_image_url(image):
        errors.append('URL изображения должен быть http(s):// и не длиннее 500 символов')

    age = form.get('age', '').strip()
    if len(age) > 20:
        errors.append('Поле «Возраст» слишком длинное (макс. 20)')

    return errors, name, price, rating, category


# ==================== EMAIL-УВЕДОМЛЕНИЯ ====================
def send_email_safe(subject, recipients, body):
    """Отправляет email; при отсутствии SMTP — только пишет в лог."""
    if not app.config.get('MAIL_SERVER'):
        app.logger.info(f'[mail] SMTP не настроен, пропуск: {subject} → {recipients}')
        return False
    if not recipients:
        return False
    try:
        msg = Message(subject=subject, recipients=recipients, body=body)
        mail.send(msg)
        return True
    except Exception as e:
        app.logger.warning(f'[mail] ошибка отправки «{subject}»: {e}')
        return False


def _order_email_body(order, is_new=True):
    lines = [f'Здравствуйте, {order.full_name}!', '']
    if is_new:
        lines.append(f'Спасибо за заказ #{order.id} в магазине «Мир Игрушек».')
    else:
        lines.append(f'Статус вашего заказа #{order.id} изменён на «{order.status}».')
    lines.append('')
    lines.append(f'Сумма: {order.total} ₽')
    lines.append(f'Статус: {order.status}')
    lines.append(f'Адрес доставки: {order.address}')
    lines.append(f'Оплата: {PAYMENT_METHODS.get(order.payment_method, order.payment_method)}')
    lines.append('')
    lines.append('Состав заказа:')
    for item in order.items:
        lines.append(f'  • {item.toy_name} × {item.quantity} = {item.subtotal} ₽')
    lines.append('')
    try:
        order_url = url_for('order_view', token=order.token, _external=True)
        lines.append(f'Ссылка на заказ: {order_url}')
        lines.append('')
    except Exception:
        pass
    lines.append('С уважением, Мир Игрушек 🧸')
    return '\n'.join(lines)


def notify_admin_new_order(order):
    if not ADMIN_EMAIL:
        return
    try:
        admin_url = url_for('admin_order_detail', order_id=order.id, _external=True)
    except Exception:
        admin_url = '(ссылка недоступна)'
    body = (
        f'Новый заказ #{order.id}\n\n'
        f'Клиент: {order.full_name}\n'
        f'Телефон: {order.phone}\n'
        f'Email: {order.email or "не указан"}\n'
        f'Адрес: {order.address}\n'
        f'Сумма: {order.total} ₽\n'
        f'Оплата: {PAYMENT_METHODS.get(order.payment_method, order.payment_method)}\n\n'
        f'Открыть в админке: {admin_url}\n'
    )
    send_email_safe(
        f'[Мир Игрушек] Новый заказ #{order.id} на {order.total} ₽',
        [ADMIN_EMAIL], body
    )


def notify_customer_new_order(order):
    if not order.email:
        return
    send_email_safe(
        f'Заказ #{order.id} оформлен — Мир Игрушек',
        [order.email],
        _order_email_body(order, is_new=True)
    )


def notify_customer_status_changed(order):
    if not order.email:
        return
    send_email_safe(
        f'Статус заказа #{order.id}: {order.status}',
        [order.email],
        _order_email_body(order, is_new=False)
    )


# ==================== КОНТЕКСТ ШАБЛОНОВ ====================
@app.context_processor
def inject_globals():
    return {
        'cart_count': cart_total_items(),
        'is_admin': is_admin(),
        'my_orders_count': len(my_order_tokens()),
        'shop_address': SHOP_ADDRESS,
        'shop_phone': SHOP_PHONE,
        'payment_methods': PAYMENT_METHODS,
        'deletable_statuses': DELETABLE_STATUSES,
        'toy_url': lambda toy: url_for('product',
                                       toy_id=toy.id,
                                       slug=toy_slug(toy)),
    }


# ==================== МОНИТОРИНГ ПРОИЗВОДИТЕЛЬНОСТИ ====================
_metrics_queue = queue.Queue(maxsize=10000)
_metrics_flusher_started = False


@app.before_request
def _start_timer():
    g._req_start = time.perf_counter()


@app.after_request
def _record_metric(response):
    """Замеряем длительность и КЛАДЁМ В ОЧЕРЕДЬ (без записи в БД!)."""
    path = request.path or ''

    if request.endpoint == 'static':
        return response
    if any(path.startswith(p) for p in MONITORING_IGNORE_PREFIXES):
        return response

    started = getattr(g, '_req_start', None)
    if started is None:
        return response

    duration_ms = (time.perf_counter() - started) * 1000.0
    endpoint = request.endpoint or 'unknown'
    path_rule = request.url_rule.rule if request.url_rule else path

    try:
        _metrics_queue.put_nowait({
            'endpoint': endpoint,
            'method': request.method,
            'path_rule': path_rule,
            'path': path[:300],
            'status': response.status_code,
            'duration_ms': duration_ms,
            'created_at': datetime.now(timezone.utc),
        })
    except queue.Full:
        pass

    response.headers['X-Response-Time'] = f'{duration_ms:.2f}ms'
    return response


def _start_metrics_flusher():
    """Фоновый поток, раз в METRICS_FLUSH_INTERVAL сбрасывает метрики в БД."""
    global _metrics_flusher_started
    if _metrics_flusher_started:
        return
    _metrics_flusher_started = True

    def worker():
        while True:
            time.sleep(METRICS_FLUSH_INTERVAL)
            try:
                _flush_metrics_batch()
            except Exception as e:
                print(f'[metrics flusher] ошибка: {e}')
                try:
                    with app.app_context():
                        db.session.rollback()
                except Exception:
                    pass

    t = threading.Thread(target=worker, name='metrics-flusher', daemon=True)
    t.start()


def _flush_metrics_batch():
    """Достаёт накопленные метрики из очереди и пишет их в БД одним батчем."""
    items = []
    while len(items) < METRICS_FLUSH_MAX_BATCH:
        try:
            items.append(_metrics_queue.get_nowait())
        except queue.Empty:
            break

    if not items:
        return

    deltas = {}
    for it in items:
        ep = it['endpoint']
        d = deltas.get(ep)
        if d is None:
            d = {
                'method': it['method'],
                'path_rule': it['path_rule'],
                'count': 0,
                'total_ms': 0.0,
                'min_ms': it['duration_ms'],
                'max_ms': it['duration_ms'],
                'errors': 0,
                'last_ms': it['duration_ms'],
                'last_status': it['status'],
                'last_called_at': it['created_at'],
            }
            deltas[ep] = d
        d['count'] += 1
        d['total_ms'] += it['duration_ms']
        if it['duration_ms'] < d['min_ms']:
            d['min_ms'] = it['duration_ms']
        if it['duration_ms'] > d['max_ms']:
            d['max_ms'] = it['duration_ms']
        d['last_ms'] = it['duration_ms']
        d['last_status'] = it['status']
        d['last_called_at'] = it['created_at']
        if it['status'] >= 500:
            d['errors'] += 1

    with app.app_context():
        existing = {
            m.endpoint: m for m in
            RouteMetric.query.filter(RouteMetric.endpoint.in_(deltas.keys())).all()
        }

        for endpoint, d in deltas.items():
            metric = existing.get(endpoint)
            if metric is None:
                metric = RouteMetric(
                    endpoint=endpoint,
                    method=d['method'],
                    path_rule=d['path_rule'],
                    count=0, total_ms=0.0,
                    min_ms=d['min_ms'], max_ms=d['max_ms'],
                    errors=0,
                )
                db.session.add(metric)
            metric.count += d['count']
            metric.total_ms += d['total_ms']
            metric.min_ms = min(metric.min_ms or d['min_ms'], d['min_ms'])
            metric.max_ms = max(metric.max_ms, d['max_ms'])
            metric.last_ms = d['last_ms']
            metric.last_status = d['last_status']
            metric.last_called_at = d['last_called_at']
            metric.errors += d['errors']

        db.session.bulk_insert_mappings(RequestLog, [{
            'endpoint': it['endpoint'],
            'method': it['method'],
            'path': it['path'],
            'status': it['status'],
            'duration_ms': it['duration_ms'],
            'created_at': it['created_at'],
        } for it in items])

        total = db.session.query(db.func.count(RequestLog.id)).scalar() or 0
        if total > REQUEST_LOG_KEEP:
            extra = total - REQUEST_LOG_KEEP
            old_ids = [
                r[0] for r in db.session.query(RequestLog.id)
                .order_by(RequestLog.id.asc()).limit(extra).all()
            ]
            if old_ids:
                RequestLog.query.filter(RequestLog.id.in_(old_ids)).delete(
                    synchronize_session=False
                )

        db.session.commit()


# ==================== ЗАГОЛОВКИ ====================
@app.after_request
def set_security_headers(response):
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


# ==================== СЛОВАРИ ДЛЯ ГЕНЕРАЦИИ ====================
_TOY_PREFIXES = [
    'Плюшевый', 'Мягкий', 'Развивающий', 'Интерактивный', 'Деревянный',
    'Магнитный', 'Светящийся', 'Музыкальный', 'Красочный', 'Весёлый',
    'Умный', 'Мини', 'Большой', 'Подарочный', 'Коллекционный',
    'Говорящий', 'Сборный', 'Сенсорный', 'Тактильный', 'Надувной',
]

_TOY_NOUNS = {
    'Мягкие игрушки': ['мишка', 'зайчик', 'котик', 'собачка', 'лисёнок',
                       'ёжик', 'пингвин', 'слонёнок', 'жирафик', 'осьминожек',
                       'единорог', 'дракончик', 'хомячок', 'козлёнок', 'овечка'],
    'Конструкторы': ['конструктор', 'набор', 'звёздолёт', 'робот', 'крепость',
                     'город', 'поезд', 'машинка', 'мега-блок', 'лабиринт',
                     'башня', 'замок', 'мост', 'краностроитель'],
    'Куклы': ['кукла', 'пупс', 'принцесса', 'фея', 'русалочка',
              'балерина', 'модница', 'малышка', 'кукла-марионетка',
              'кукла-рукавичка', 'пупс-голыш'],
    'Машинки': ['машинка', 'грузовик', 'внедорожник', 'гоночный болид',
                'пожарная', 'скорая', 'трактор', 'экскаватор', 'самосвал',
                'вертолёт', 'самолёт', 'поезд', 'катер', 'танк'],
    'Пазлы': ['пазл', 'мозаика', 'головоломка', '3D-пазл', 'деревянный пазл',
              'пазл-чемодан', 'пазл-коврик', 'пазл-сфера', 'пазл-картина'],
    'Для малышей': ['погремушка', 'прорезыватель', 'коврик', 'ходунки',
                    'сортер', 'шнуровка', 'бизиборд', 'каталка', 'неваляшка',
                    'юла', 'молоточек', 'кубики'],
    'Творчество': ['набор для лепки', 'набор для рисования', 'гравюра',
                   'витраж', 'фреска', 'мыло-набор', 'свечи-набор',
                   'квиллинг', 'оригами', 'аппликация', 'роспись по камню'],
    'Настольные игры': ['настольная игра', 'лото', 'домино', 'мемо',
                        'ходилка', 'викторина', 'стратегия', 'карточная игра',
                        'квест-игра', 'игра-бродилка'],
    'Спорт и активный отдых': ['мяч', 'скакалка', 'бадминтон', 'обруч',
                               'самокат', 'ролики', 'боксёрская груша',
                               'дартс', 'боулинг', 'фрисби', 'батут'],
    'Электронные игрушки': ['радиоуправляемый', 'интерактивный робот',
                            'дрон', 'умный питомец', 'электронная игра',
                            'говорящая азбука', 'музыкальный коврик'],
}

_AGE_GROUPS = ['0+', '1+', '2+', '3+', '4+', '5+', '6+', '7+', '8+', '9+', '10+', '12+']

_IMAGE_POOL = [
    'https://images.unsplash.com/photo-1562040506-b4d5b6f9e3d0?w=500',
    'https://images.unsplash.com/photo-1587654780291-39c9404d746b?w=500',
    'https://images.unsplash.com/photo-1595608435350-cba6e8be7b8e?w=500',
    'https://images.unsplash.com/photo-1594736797933-d0501ba2fe65?w=500',
    'https://images.unsplash.com/photo-1611996575749-79a3a250f948?w=500',
    'https://images.unsplash.com/photo-1522771930-78848d9293e8?w=500',
    'https://images.unsplash.com/photo-1513364776144-60967b0f800f?w=500',
    'https://images.unsplash.com/photo-1596461404969-9ae70f2830c1?w=500',
    'https://images.unsplash.com/photo-1558060370-d644479cb6f7?w=500',
    'https://images.unsplash.com/photo-1545558014-8692077e9b5c?w=500',
    'https://images.unsplash.com/photo-1526304640581-d334cdbbf45e?w=500',
]

_PRICE_POOL = [290, 390, 490, 590, 690, 890, 990, 1290, 1490, 1790, 1990,
               2490, 2990, 3490, 3990, 4490, 4990, 5990, 6990, 8990, 9990, 12990]

_NAMES = [
    'Иванов Иван', 'Петров Пётр', 'Сидоров Сидор', 'Кузнецов Алексей',
    'Смирнова Анна', 'Попова Мария', 'Соколова Екатерина', 'Морозов Дмитрий',
    'Новиков Сергей', 'Фёдорова Ольга', 'Егоров Артём', 'Павлова Дарья',
    'Козлов Никита', 'Волкова Юлия', 'Соловьёв Максим', 'Васильева Елена',
    'Зайцев Роман', 'Павлов Кирилл', 'Семёнова Виктория', 'Голубев Андрей',
    'Виноградова Полина', 'Богданов Егор', 'Воробьёва Алиса', 'Фролов Матвей',
    'Михайлова София', 'Беляев Тимофей', 'Тарасов Лев', 'Белова Ксения',
    'Комаров Марк', 'Орлова Варвара', 'Киселёв Мирон', 'Макарова Милана',
    'Андреев Александр', 'Ковалёва Анастасия', 'Ильин Владислав',
    'Гусева Вероника', 'Титов Даниил', 'Куликова Арина', 'Алексеев Илья',
    'Степанова Ева', 'Яковлев Родион', 'Никитина Амелия', 'Сорокин Лев',
    'Баранов Савелий', 'Филиппов Глеб', 'Марков Степан', 'Большаков Тимур',
    'Крылов Ярослав', 'Никифорова Кира', 'Громов Елисей',
]

_ADDRESSES = [
    'г. Ижевск, ул. Труда, д. 8, кв. 1', 'г. Ижевск, ул. Пушкинская, д. 15, кв. 42',
    'г. Ижевск, ул. Ленина, д. 100, кв. 7', 'г. Ижевск, ул. Советская, д. 22, кв. 15',
    'г. Ижевск, ул. К. Маркса, д. 33, кв. 88', 'г. Ижевск, ул. Удмуртская, д. 250, кв. 5',
    'г. Ижевск, ул. Автозаводская, д. 45, кв. 12', 'г. Ижевск, ул. Воткинское шоссе, д. 130, кв. 56',
    'г. Ижевск, ул. Молодёжная, д. 78, кв. 33', 'г. Ижевск, ул. Союзная, д. 12, кв. 21',
    'г. Ижевск, ул. 40 лет Победы, д. 55, кв. 90', 'г. Ижевск, ул. Баранова, д. 88, кв. 3',
    'г. Ижевск, ул. Маяковского, д. 27, кв. 44', 'г. Ижевск, ул. Кирова, д. 118, кв. 6',
    'г. Ижевск, ул. Первомайская, д. 33, кв. 17', 'г. Ижевск, ул. Дзержинского, д. 5, кв. 62',
    'г. Ижевск, ул. Красноармейская, д. 143, кв. 8', 'г. Ижевск, ул. Постольский тракт, д. 3, кв. 11',
    'г. Ижевск, ул. Салютовская, д. 30, кв. 25', 'г. Ижевск, ул. Тимирязева, д. 41, кв. 39',
]


# ==================== ГЕНЕРАЦИЯ МАССОВЫХ ДАННЫХ ====================
def _bulk_generate_toys(count):
    categories = list(_TOY_NOUNS.keys())
    toys_buffer = []

    for i in range(count):
        cat = random.choice(categories)
        noun = random.choice(_TOY_NOUNS[cat])
        prefix = random.choice(_TOY_PREFIXES)
        suffix = random.choice(['', ' детский', ' для детей', ' подарочный'])

        name = f'{prefix} {noun}{suffix} #{i + 1}'

        toys_buffer.append({
            'name': name[:200],
            'price': random.choice(_PRICE_POOL),
            'category': cat,
            'age': random.choice(_AGE_GROUPS),
            'description': (f'{name.capitalize()} — отличный выбор для детей. '
                            f'Развивает воображение, моторику и усидчивость. '
                            f'Соответствует стандартам безопасности.'),
            'image': random.choice(_IMAGE_POOL),
            'rating': round(random.uniform(3.5, 5.0), 1),
        })

        if len(toys_buffer) >= SEED_BATCH_SIZE:
            db.session.bulk_insert_mappings(Toy, toys_buffer)
            db.session.commit()
            toys_buffer.clear()

    if toys_buffer:
        db.session.bulk_insert_mappings(Toy, toys_buffer)
        db.session.commit()


def _bulk_generate_orders(count):
    orders_buffer = []
    base_time = datetime.now(timezone.utc) - timedelta(days=730)

    for _ in range(count):
        status = random.choice(ORDER_STATUSES)
        orders_buffer.append({
            'token': secrets.token_urlsafe(32),
            'created_at': base_time + timedelta(
                seconds=random.randint(0, 730 * 24 * 3600)
            ),
            'full_name': random.choice(_NAMES),
            'phone': (f'+7 ({random.randint(100, 999)}) '
                      f'{random.randint(100, 999)}-'
                      f'{random.randint(10, 99)}-'
                      f'{random.randint(10, 99)}'),
            'email': '',
            'address': random.choice(_ADDRESSES),
            'payment_method': random.choice(list(PAYMENT_METHODS.keys())),
            'total': 0,
            'status': status,
            'cancel_reason': ('Клиент передумал' if status == 'Отменён' else ''),
        })

        if len(orders_buffer) >= SEED_BATCH_SIZE:
            db.session.bulk_insert_mappings(Order, orders_buffer)
            db.session.commit()
            orders_buffer.clear()

    if orders_buffer:
        db.session.bulk_insert_mappings(Order, orders_buffer)
        db.session.commit()

    order_ids = [row[0] for row in db.session.query(Order.id).all()]
    if not order_ids:
        return

    toy_pool = db.session.query(Toy.id, Toy.name, Toy.price).all()
    if not toy_pool:
        return

    items_buffer = []

    for oid in order_ids:
        n_items = random.randint(SEED_ITEMS_PER_ORDER_MIN, SEED_ITEMS_PER_ORDER_MAX)
        chosen = random.sample(toy_pool, min(n_items, len(toy_pool)))
        for (tid, tname, tprice) in chosen:
            qty = random.randint(1, 3)
            subtotal = tprice * qty
            items_buffer.append({
                'order_id': oid,
                'toy_name': tname,
                'price': tprice,
                'quantity': qty,
                'subtotal': subtotal,
            })

        if len(items_buffer) >= SEED_BATCH_SIZE:
            db.session.bulk_insert_mappings(OrderItem, items_buffer)
            db.session.commit()
            items_buffer.clear()

    if items_buffer:
        db.session.bulk_insert_mappings(OrderItem, items_buffer)
        db.session.commit()

    db.session.execute(db.text(
        "UPDATE orders SET total = COALESCE("
        "(SELECT SUM(subtotal) FROM order_items WHERE order_items.order_id = orders.id)"
        ", 0)"
    ))
    db.session.commit()


# ==================== ПРОСТАЯ МИГРАЦИЯ ====================
def _ensure_columns():
    """Добавляет новые колонки в существующие таблицы (без Alembic)."""
    try:
        with db.engine.begin() as conn:
            cols = [row[1] for row in
                    conn.execute(db.text("PRAGMA table_info(orders)")).fetchall()]
            if 'email' not in cols:
                conn.execute(db.text(
                    "ALTER TABLE orders ADD COLUMN email VARCHAR(200) DEFAULT ''"
                ))
                print('✅ Миграция: добавлена колонка orders.email')
    except Exception as e:
        print(f'⚠️  Миграция колонок: {e}')


# ==================== ИНИЦИАЛИЗАЦИЯ БД ====================
def init_db():
    db.create_all()
    _ensure_columns()

    demo_toys = [
        {'name': 'Плюшевый мишка Тёма', 'price': 1490, 'category': 'Мягкие игрушки',
         'age': '3+', 'description': 'Мягкий и уютный медвежонок из гипоаллергенного материала. Высота 40 см.',
         'image': 'https://images.unsplash.com/photo-1562040506-b4d5b6f9e3d0?w=500', 'rating': 4.8},
        {'name': 'Конструктор "Космический корабль"', 'price': 2990, 'category': 'Конструкторы',
         'age': '6+', 'description': 'Развивающий конструктор из 450 деталей. Собери свой космический корабль!',
         'image': 'https://images.unsplash.com/photo-1587654780291-39c9404d746b?w=500', 'rating': 4.9},
        {'name': 'Кукла Маша', 'price': 1990, 'category': 'Куклы',
         'age': '3+', 'description': 'Красивая кукла с длинными волосами и набором одежды. Высота 35 см.',
         'image': 'https://images.unsplash.com/photo-1595608435350-cba6e8be7b8e?w=500', 'rating': 4.7},
        {'name': 'Машинка на радиоуправлении', 'price': 3490, 'category': 'Машинки',
         'age': '5+', 'description': 'Скоростная машинка с пультом управления. Работает до 30 минут без подзарядки.',
         'image': 'https://images.unsplash.com/photo-1594736797933-d0501ba2fe65?w=500', 'rating': 4.6},
        {'name': 'Пазл "Динозавры" 1000 деталей', 'price': 890, 'category': 'Пазлы',
         'age': '7+', 'description': 'Яркий пазл с динозаврами. Отличная тренировка внимания и усидчивости.',
         'image': 'https://images.unsplash.com/photo-1611996575749-79a3a250f948?w=500', 'rating': 4.5},
        {'name': 'Развивающий коврик', 'price': 2450, 'category': 'Для малышей',
         'age': '0+', 'description': 'Мягкий коврик с дугами, погремушками и зеркальцем для новорождённых.',
         'image': 'https://images.unsplash.com/photo-1522771930-78848d9293e8?w=500', 'rating': 4.9},
        {'name': 'Набор юного художника', 'price': 1590, 'category': 'Творчество',
         'age': '4+', 'description': '60 предметов: краски, кисти, карандаши, фломастеры и альбом.',
         'image': 'https://images.unsplash.com/photo-1513364776144-60967b0f800f?w=500', 'rating': 4.8},
        {'name': 'Железная дорога "Экспресс"', 'price': 4290, 'category': 'Конструкторы',
         'age': '5+', 'description': 'Большой набор железной дороги с поездом на батарейках и станциями.',
         'image': 'https://images.unsplash.com/photo-1596461404969-9ae70f2830c1?w=500', 'rating': 4.7},
    ]

    existing_names = {t[0] for t in db.session.query(Toy.name).all()}
    added_demo = 0
    for t in demo_toys:
        if t['name'] not in existing_names:
            db.session.add(Toy(**t))
            added_demo += 1
    if added_demo:
        db.session.commit()
        print(f'✅ Добавлено {added_demo} демо-товаров')

    toys_current = Toy.query.count()
    if toys_current < SEED_TOYS_COUNT:
        to_add = SEED_TOYS_COUNT - toys_current
        print(f'⏳ Генерация {to_add} товаров...')
        t0 = time.perf_counter()
        _bulk_generate_toys(to_add)
        print(f'   ✓ товары: {to_add} за {time.perf_counter() - t0:.2f} с')

    orders_current = Order.query.count()
    if orders_current < SEED_ORDERS_COUNT:
        to_add = SEED_ORDERS_COUNT - orders_current
        print(f'⏳ Генерация {to_add} заказов...')
        t0 = time.perf_counter()
        _bulk_generate_orders(to_add)
        print(f'   ✓ заказы: {to_add} за {time.perf_counter() - t0:.2f} с')

    print(f'📊 Итог БД: товаров={Toy.query.count()}, '
          f'заказов={Order.query.count()}, '
          f'позиций={OrderItem.query.count()}')

    try:
        mode = db.session.execute(db.text("PRAGMA journal_mode")).scalar()
        print(f'🔧 SQLite journal_mode: {mode}')
    except Exception as e:
        print(f'⚠️  Не удалось проверить journal_mode: {e}')


# ==================== ОСНОВНЫЕ МАРШРУТЫ ====================
@app.route('/')
def index():
    featured = Toy.query.order_by(Toy.rating.desc(), Toy.id.desc()).limit(4).all()
    categories = get_categories_cached()
    return render_template('index.html', featured=featured, categories=categories)


@app.route('/catalog')
def catalog():
    category = request.args.get('category', '')
    search = request.args.get('search', '').strip()
    sort = request.args.get('sort', '')
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1

    query = Toy.query

    if category:
        query = query.filter(Toy.category == category)
    if search:
        safe_search = (search
                       .replace('\\', '\\\\')
                       .replace('%', '\\%')
                       .replace('_', '\\_'))
        like = f'%{safe_search}%'
        query = query.filter(db.or_(
            Toy.name.ilike(like, escape='\\'),
            Toy.description.ilike(like, escape='\\'),
        ))
    if sort == 'price_asc':
        query = query.order_by(Toy.price.asc(), Toy.id.asc())
    elif sort == 'price_desc':
        query = query.order_by(Toy.price.desc(), Toy.id.asc())
    elif sort == 'rating':
        query = query.order_by(Toy.rating.desc(), Toy.id.asc())
    else:
        query = query.order_by(Toy.id.asc())

    pagination = query.paginate(
        page=page, per_page=PER_PAGE_CATALOG, error_out=False
    )
    toys = pagination.items

    categories = get_categories_cached()
    return render_template('catalog.html',
                           toys=toys,
                           pagination=pagination,
                           categories=categories,
                           current_category=category,
                           search=search,
                           sort=sort)


@app.route('/product/<int:toy_id>')
@app.route('/product/<int:toy_id>-<slug>')
def product(toy_id, slug=None):
    toy = get_toy(toy_id)
    if not toy:
        flash('Товар не найден', 'error')
        return redirect(url_for('catalog'))

    # Приводим URL к каноничному виду: /product/123-plyushevyy-mishka
    correct = toy_slug(toy)
    if slug != correct:
        return redirect(
            url_for('product', toy_id=toy.id, slug=correct),
            code=301
        )

    similar = Toy.query.filter(
        Toy.category == toy.category,
        Toy.id != toy_id
    ).limit(4).all()
    return render_template('product.html', toy=toy, similar=similar)


@app.route('/cart')
def cart():
    cart_data = get_cart()
    items, total = [], 0
    for toy_id_str, qty in cart_data.items():
        try:
            tid = int(toy_id_str)
        except (TypeError, ValueError):
            continue
        toy = get_toy(tid)
        if toy:
            subtotal = toy.price * qty
            total += subtotal
            items.append({'toy': toy, 'quantity': qty, 'subtotal': subtotal})
    return render_template('cart.html', items=items, total=total)


@app.route('/add_to_cart/<int:toy_id>', methods=['POST'])
def add_to_cart(toy_id):
    toy = get_toy(toy_id)
    if not toy:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Товар не найден'}), 404
        flash('Товар не найден', 'error')
        return redirect(url_for('catalog'))

    try:
        quantity = int(request.form.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    quantity = max(1, min(quantity, 99))

    cart_data = get_cart()
    key = str(toy_id)

    if key not in cart_data and len(cart_data) >= MAX_CART_UNIQUE_ITEMS:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Корзина переполнена'}), 400
        flash('Корзина переполнена', 'error')
        return redirect(url_for('cart'))

    new_qty = cart_data.get(key, 0) + quantity
    cart_data[key] = min(new_qty, 99)
    save_cart(cart_data)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'message': f'"{toy.name}" добавлен в корзину',
            'cart_count': cart_total_items()
        })

    flash(f'"{toy.name}" добавлен в корзину 🧸', 'success')

    if is_safe_url(request.referrer):
        return redirect(request.referrer)
    return redirect(url_for('catalog'))


@app.route('/update_cart/<int:toy_id>', methods=['POST'])
def update_cart(toy_id):
    toy = get_toy(toy_id)
    if not toy:
        flash('Товар не найден', 'error')
        return redirect(url_for('cart'))

    try:
        quantity = int(request.form.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    quantity = max(0, min(quantity, 99))

    cart_data = get_cart()
    key = str(toy_id)
    if quantity == 0:
        cart_data.pop(key, None)
    else:
        cart_data[key] = quantity
    save_cart(cart_data)
    return redirect(url_for('cart'))


@app.route('/remove_from_cart/<int:toy_id>', methods=['POST'])
def remove_from_cart(toy_id):
    cart_data = get_cart()
    cart_data.pop(str(toy_id), None)
    save_cart(cart_data)
    flash('Товар удалён из корзины', 'success')
    return redirect(url_for('cart'))


# ==================== ОФОРМЛЕНИЕ ЗАКАЗА ====================
@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    cart_data = get_cart()
    if not cart_data:
        flash('Корзина пуста', 'error')
        return redirect(url_for('cart'))

    items, total = [], 0
    for toy_id_str, qty in cart_data.items():
        try:
            tid = int(toy_id_str)
        except (TypeError, ValueError):
            continue
        toy = get_toy(tid)
        if toy:
            subtotal = toy.price * qty
            total += subtotal
            items.append({'toy': toy, 'quantity': qty, 'subtotal': subtotal})

    if not items:
        flash('Товары недоступны, корзина очищена', 'error')
        session['cart'] = {}
        session.modified = True
        return redirect(url_for('cart'))

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        address = request.form.get('address', '').strip()
        payment_method = request.form.get('payment_method', '').strip()

        errors = []
        if len(full_name) < 2:
            errors.append('Укажите имя и фамилию')
        if len(full_name) > 200:
            errors.append('Имя слишком длинное')
        if not re.match(r'^[\d\+\-\(\)\s]{10,20}$', phone):
            errors.append('Некорректный номер телефона')
        if email and not EMAIL_RE.match(email):
            errors.append('Некорректный email')
        if len(email) > 200:
            errors.append('Email слишком длинный')
        if len(address) < 5:
            errors.append('Укажите адрес доставки')
        if len(address) > 1000:
            errors.append('Адрес слишком длинный')
        if payment_method not in PAYMENT_METHODS:
            errors.append('Выберите способ оплаты')

        if errors:
            for err in errors:
                flash(err, 'error')
            return render_template('checkout.html', items=items, total=total,
                                   form=request.form)

        order = Order(
            token=secrets.token_urlsafe(32),
            full_name=full_name,
            phone=phone,
            email=email,
            address=address,
            payment_method=payment_method,
            total=total,
            status='Новый'
        )
        db.session.add(order)
        db.session.flush()

        for it in items:
            db.session.add(OrderItem(
                order_id=order.id,
                toy_name=it['toy'].name,
                price=it['toy'].price,
                quantity=it['quantity'],
                subtotal=it['subtotal'],
            ))
        db.session.commit()

        # Email-уведомления (не блокируют заказ при ошибке)
        try:
            notify_customer_new_order(order)
        except Exception as e:
            app.logger.warning(f'[mail] клиенту: {e}')
        try:
            notify_admin_new_order(order)
        except Exception as e:
            app.logger.warning(f'[mail] админу: {e}')

        remember_order(order.token)

        session['cart'] = {}
        session.modified = True

        return redirect(url_for('order_success', token=order.token))

    return render_template('checkout.html', items=items, total=total, form={})


@app.route('/order/success/<token>')
def order_success(token):
    order = get_order_by_token(token)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('index'))
    remember_order(order.token)
    return render_template('order_success.html', order=order)


# ==================== QR-КОД ЗАКАЗА ====================
@app.route('/order/<token>/qr.png')
def order_qr(token):
    """PNG с QR-кодом, ведущим на страницу заказа."""
    order = get_order_by_token(token)
    if not order:
        abort(404)

    order_url = url_for('order_view', token=order.token, _external=True)

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(order_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)

    resp = Response(buf.getvalue(), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ==================== МОИ ЗАКАЗЫ ====================
@app.route('/my-orders')
def my_orders():
    tokens = my_order_tokens()
    orders = []
    if tokens:
        orders = (Order.query
                  .filter(Order.token.in_(tokens))
                  .order_by(Order.id.desc())
                  .limit(PER_PAGE_MY_ORDERS)
                  .all())
    return render_template('my_orders.html', orders=orders)


@app.route('/order/<token>')
def order_view(token):
    order = get_order_by_token(token)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('my_orders'))

    can_edit = order.status in EDITABLE_STATUSES
    can_cancel = order.status in CANCELLABLE_STATUSES

    return render_template(
        'order_view.html',
        order=order,
        can_edit=can_edit,
        can_cancel=can_cancel,
    )


@app.route('/order/<token>/edit', methods=['GET', 'POST'])
def order_edit(token):
    order = get_order_by_token(token)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('my_orders'))

    if order.status not in EDITABLE_STATUSES:
        flash(f'Редактировать заказ можно только в статусе: {", ".join(EDITABLE_STATUSES)}', 'error')
        return redirect(url_for('order_view', token=order.token))

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        phone = request.form.get('phone', '').strip()
        address = request.form.get('address', '').strip()

        errors = []
        if len(full_name) < 2:
            errors.append('Укажите имя и фамилию')
        if len(full_name) > 200:
            errors.append('Имя слишком длинное')
        if not re.match(r'^[\d\+\-\(\)\s]{10,20}$', phone):
            errors.append('Некорректный телефон')
        if len(address) < 5:
            errors.append('Укажите адрес')
        if len(address) > 1000:
            errors.append('Адрес слишком длинный')

        if errors:
            for err in errors:
                flash(err, 'error')
            return render_template('order_edit.html', order=order, form=request.form)

        order.full_name = full_name
        order.phone = phone
        order.address = address
        db.session.commit()

        flash('Заказ обновлён', 'success')
        return redirect(url_for('order_view', token=order.token))

    return render_template('order_edit.html', order=order, form={})


@app.route('/order/<token>/cancel', methods=['POST'])
def order_cancel(token):
    order = get_order_by_token(token)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('my_orders'))

    if order.status not in CANCELLABLE_STATUSES:
        flash('Этот заказ уже нельзя отменить', 'error')
        return redirect(url_for('order_view', token=order.token))

    reason = request.form.get('reason', '').strip()
    order.status = 'Отменён'
    order.cancel_reason = reason[:300]
    db.session.commit()

    # Уведомляем клиента об отмене
    try:
        notify_customer_status_changed(order)
    except Exception as e:
        app.logger.warning(f'[mail] отмена: {e}')

    flash('Заказ отменён', 'success')
    return redirect(url_for('order_view', token=order.token))


# ==================== АДМИН-ПАНЕЛЬ ====================
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if is_admin():
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        login = request.form.get('login', '').strip()
        password = request.form.get('password', '').strip()

        if (secrets.compare_digest(login, ADMIN_LOGIN)
                and check_password_hash(ADMIN_PASSWORD_HASH, password)):
            session['is_admin'] = True
            session.permanent = True
            flash('Вы вошли как администратор', 'success')
            return redirect(url_for('admin_dashboard'))

        flash('Неверный логин или пароль', 'error')

    return render_template('admin/login.html')


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('is_admin', None)
    flash('Вы вышли из админ-панели', 'success')
    return redirect(url_for('index'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    toys_count = db.session.query(db.func.count(Toy.id)).scalar() or 0
    orders_count = db.session.query(db.func.count(Order.id)).scalar() or 0

    row = db.session.execute(db.text(
        "SELECT "
        "(SELECT COALESCE(SUM(price), 0) FROM toys) AS total_value, "
        "(SELECT COALESCE(SUM(total), 0) FROM orders) AS orders_sum"
    )).fetchone()
    total_value = row[0] if row else 0
    orders_sum = row[1] if row else 0

    stats = {
        'toys_count': toys_count,
        'orders_count': orders_count,
        'total_value': total_value,
        'orders_sum': orders_sum,
    }

    recent_orders = (Order.query
                     .order_by(Order.id.desc())
                     .limit(5)
                     .all())
    return render_template('admin/dashboard.html', stats=stats, recent_orders=recent_orders)


@app.route('/admin/products')
@admin_required
def admin_products():
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1

    pagination = (Toy.query
                  .order_by(Toy.id.asc())
                  .paginate(page=page, per_page=PER_PAGE_ADMIN_PRODUCTS, error_out=False))

    return render_template('admin/products.html',
                           toys=pagination.items,
                           pagination=pagination)


@app.route('/admin/products/new', methods=['GET', 'POST'])
@admin_required
def admin_product_new():
    if request.method == 'POST':
        errors, name, price, rating, category = _validate_toy_form(request.form)
        if errors:
            for err in errors:
                flash(err, 'error')
            return render_template('admin/product_form.html', toy=request.form, mode='new')

        toy = Toy(
            name=name,
            price=price,
            category=category,
            age=request.form.get('age', '').strip() or '3+',
            description=request.form.get('description', '').strip(),
            image=request.form.get('image', '').strip() or
                  'https://via.placeholder.com/500?text=No+Image',
            rating=rating,
        )
        db.session.add(toy)
        db.session.commit()
        invalidate_categories_cache()
        flash(f'Товар "{toy.name}" добавлен', 'success')
        return redirect(url_for('admin_products'))

    return render_template('admin/product_form.html', toy={}, mode='new')


@app.route('/admin/products/<int:toy_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_product_edit(toy_id):
    toy = get_toy(toy_id)
    if not toy:
        flash('Товар не найден', 'error')
        return redirect(url_for('admin_products'))

    if request.method == 'POST':
        errors, name, price, rating, category = _validate_toy_form(request.form)
        if errors:
            for err in errors:
                flash(err, 'error')
            return render_template('admin/product_form.html', toy=request.form, mode='edit')

        toy.name = name
        toy.price = price
        toy.rating = rating
        toy.category = category
        toy.age = request.form.get('age', toy.age).strip()
        toy.description = request.form.get('description', toy.description).strip()
        toy.image = request.form.get('image', toy.image).strip()

        db.session.commit()
        invalidate_categories_cache()
        flash('Товар обновлён', 'success')
        return redirect(url_for('admin_products'))

    return render_template('admin/product_form.html', toy=toy, mode='edit')


@app.route('/admin/products/<int:toy_id>/delete', methods=['POST'])
@admin_required
def admin_product_delete(toy_id):
    toy = get_toy(toy_id)
    if toy:
        name = toy.name
        db.session.delete(toy)
        db.session.commit()
        invalidate_categories_cache()
        flash(f'Товар "{name}" удалён', 'success')
    else:
        flash('Товар не найден', 'error')
    return redirect(url_for('admin_products'))


@app.route('/admin/orders')
@admin_required
def admin_orders():
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1

    status_filter = request.args.get('status', '').strip()

    query = Order.query
    if status_filter in ORDER_STATUSES:
        query = query.filter(Order.status == status_filter)

    pagination = (query
                  .order_by(Order.id.desc())
                  .paginate(page=page, per_page=PER_PAGE_ADMIN_ORDERS, error_out=False))

    return render_template('admin/orders.html',
                           orders=pagination.items,
                           pagination=pagination,
                           statuses=ORDER_STATUSES,
                           current_status=status_filter)


@app.route('/admin/orders/<int:order_id>')
@admin_required
def admin_order_detail(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('admin_orders'))
    _ = order.items
    return render_template('admin/order_detail.html', order=order, statuses=ORDER_STATUSES)


@app.route('/admin/orders/<int:order_id>/status', methods=['POST'])
@admin_required
def admin_order_status(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('admin_orders'))

    new_status = request.form.get('status', '').strip()
    if new_status not in ORDER_STATUSES:
        flash('Недопустимый статус заказа', 'error')
        return redirect(url_for('admin_order_detail', order_id=order_id))

    old_status = order.status
    order.status = new_status
    db.session.commit()

    if old_status != new_status:
        try:
            notify_customer_status_changed(order)
        except Exception as e:
            app.logger.warning(f'[mail] статус: {e}')

    flash('Статус заказа обновлён', 'success')
    return redirect(url_for('admin_order_detail', order_id=order_id))


@app.route('/admin/orders/<int:order_id>/delete', methods=['POST'])
@admin_required
def admin_order_delete(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('admin_orders'))

    if order.status not in DELETABLE_STATUSES:
        flash(
            f'Удалить можно только заказы в статусах: {", ".join(DELETABLE_STATUSES)}',
            'error'
        )
        return redirect(url_for('admin_order_detail', order_id=order_id))

    db.session.delete(order)
    db.session.commit()

    flash(f'Заказ #{order_id} удалён', 'success')
    return redirect(url_for('admin_orders'))


# ==================== МОНИТОРИНГ: СТРАНИЦА И API ====================
@app.route('/admin/performance')
@admin_required
def admin_performance():
    return render_template('admin/performance.html')


@app.route('/admin/api/route-metrics')
@admin_required
def admin_api_route_metrics():
    try:
        _flush_metrics_batch()
    except Exception as e:
        app.logger.warning(f'[metrics] принудительный flush: {e}')

    metrics = RouteMetric.query.order_by(RouteMetric.count.desc()).all()
    logs = (RequestLog.query
            .order_by(RequestLog.id.desc())
            .limit(30).all())

    total_requests = sum(m.count for m in metrics)
    total_errors = sum(m.errors for m in metrics)
    all_avg = (
        round(sum(m.total_ms for m in metrics) / total_requests, 2)
        if total_requests else 0
    )
    slowest = max(metrics, key=lambda m: m.avg_ms, default=None)

    resp = jsonify({
        'summary': {
            'endpoints': len(metrics),
            'total_requests': total_requests,
            'total_errors': total_errors,
            'avg_ms': all_avg,
            'slowest_endpoint': slowest.endpoint if slowest else '—',
            'slowest_avg_ms': slowest.avg_ms if slowest else 0,
        },
        'routes': [{
            'endpoint': m.endpoint,
            'method': m.method,
            'path_rule': m.path_rule,
            'count': m.count,
            'avg_ms': round(m.avg_ms, 2),
            'min_ms': round(m.min_ms or 0, 2),
            'max_ms': round(m.max_ms or 0, 2),
            'last_ms': round(m.last_ms or 0, 2),
            'last_status': m.last_status,
            'errors': m.errors,
            'last_called_at': (m.last_called_at.strftime('%H:%M:%S')
                               if m.last_called_at else '—'),
        } for m in metrics],
        'recent': [{
            'endpoint': l.endpoint,
            'method': l.method,
            'path': l.path,
            'status': l.status,
            'duration_ms': round(l.duration_ms or 0, 2),
            'time': l.created_at.strftime('%H:%M:%S') if l.created_at else '—',
        } for l in logs],
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/admin/api/route-metrics/reset', methods=['POST'])
@admin_required
def admin_api_route_metrics_reset():
    RouteMetric.query.delete()
    RequestLog.query.delete()
    db.session.commit()
    return jsonify({'success': True})


# ==================== ОБРАБОТЧИКИ ОШИБОК ====================
@app.errorhandler(404)
def not_found(e):
    try:
        return render_template('404.html'), 404
    except Exception:
        return '<h1>404</h1><p>Страница не найдена</p>', 404


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    flash('Сессия истекла. Обновите страницу и попробуйте снова.', 'error')
    return redirect(request.referrer or url_for('index'))


@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    try:
        return render_template('500.html'), 500
    except Exception:
        return '<h1>500</h1><p>Внутренняя ошибка сервера</p>', 500


# ==================== ЗАПУСК ====================
with app.app_context():
    init_db()
    _start_metrics_flusher()


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', 5055))
    app.run(debug=debug_mode, host=host, port=port, threaded=True)