from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from dotenv import load_dotenv
import re
import os
import secrets

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

# ==================== БЕЗОПАСНОСТЬ COOKIE ====================
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,           # True только с HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    WTF_CSRF_TIME_LIMIT=7200,              # 2 часа в СЕКУНДАХ (не timedelta!)
    WTF_CSRF_HEADERS=['X-CSRFToken', 'X-CSRF-Token'],
)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)

# ==================== НАСТРОЙКИ ====================
ADMIN_LOGIN = os.environ.get('ADMIN_LOGIN', 'admin')

_admin_plain = os.environ.get('ADMIN_PASSWORD')
if not _admin_plain:
    raise RuntimeError('ADMIN_PASSWORD не задан в .env')

ADMIN_PASSWORD_HASH = generate_password_hash(_admin_plain)

SHOP_ADDRESS = 'г. Ижевск, ул. Труда 8'
SHOP_PHONE = '+7 (3412) 45-67-89'

ORDER_STATUSES = ['Новый', 'Оплачен', 'Собирается', 'Отправлен', 'Доставлен', 'Отменён']
CANCELLABLE_STATUSES = ['Новый', 'Оплачен']
EDITABLE_STATUSES = ['Новый']
DELETABLE_STATUSES = ['Отменён', 'Доставлен']   # удаление заказа админом

MAX_CART_UNIQUE_ITEMS = 50
MAX_IMAGE_URL_LEN = 500

PAYMENT_METHODS = {
    'card_on_delivery': 'Оплата картой при получении',
    'cash_on_delivery': 'Оплата наличными при получении',
    'online_demo': 'Онлайн-оплата (демо)',
}


# ==================== МОДЕЛИ ====================
class Toy(db.Model):
    __tablename__ = 'toys'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(100), nullable=False, index=True)
    age = db.Column(db.String(20), default='3+')
    description = db.Column(db.Text, default='')
    image = db.Column(db.String(500), default='')
    rating = db.Column(db.Float, default=5.0, index=True)


class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    full_name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    address = db.Column(db.Text, nullable=False)
    payment_method = db.Column(db.String(50), nullable=False, default='card_on_delivery')
    total = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(50), default='Новый', index=True)
    cancel_reason = db.Column(db.String(300), default='')

    items = db.relationship('OrderItem', backref='order', cascade='all, delete-orphan')


class OrderItem(db.Model):
    __tablename__ = 'order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    toy_name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    subtotal = db.Column(db.Integer, nullable=False)


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
    }


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
        "script-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


# ==================== ИНИЦИАЛИЗАЦИЯ БД ====================
def init_db():
    db.create_all()

    if Toy.query.count() > 0:
        return

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

    for t in demo_toys:
        db.session.add(Toy(**t))
    db.session.commit()
    print(f'✅ База данных инициализирована: добавлено {len(demo_toys)} товаров')


# ==================== ОСНОВНЫЕ МАРШРУТЫ ====================
@app.route('/')
def index():
    featured = Toy.query.order_by(Toy.rating.desc()).limit(4).all()
    categories = [c[0] for c in
                  db.session.query(Toy.category).distinct().order_by(Toy.category).all()]
    return render_template('index.html', featured=featured, categories=categories)


@app.route('/catalog')
def catalog():
    category = request.args.get('category', '')
    search = request.args.get('search', '').strip()
    sort = request.args.get('sort', '')

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
        query = query.order_by(Toy.price.asc())
    elif sort == 'price_desc':
        query = query.order_by(Toy.price.desc())
    elif sort == 'rating':
        query = query.order_by(Toy.rating.desc())

    toys = query.all()
    categories = [c[0] for c in
                  db.session.query(Toy.category).distinct().order_by(Toy.category).all()]
    return render_template('catalog.html', toys=toys, categories=categories,
                           current_category=category, search=search, sort=sort)


@app.route('/product/<int:toy_id>')
def product(toy_id):
    toy = get_toy(toy_id)
    if not toy:
        flash('Товар не найден', 'error')
        return redirect(url_for('catalog'))

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
        address = request.form.get('address', '').strip()
        payment_method = request.form.get('payment_method', '').strip()

        errors = []
        if len(full_name) < 2:
            errors.append('Укажите имя и фамилию')
        if len(full_name) > 200:
            errors.append('Имя слишком длинное')
        if not re.match(r'^[\d\+\-\(\)\s]{10,20}$', phone):
            errors.append('Некорректный номер телефона')
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


# ==================== МОИ ЗАКАЗЫ ====================
@app.route('/my-orders')
def my_orders():
    tokens = my_order_tokens()
    orders = []
    if tokens:
        orders = Order.query.filter(Order.token.in_(tokens)).order_by(Order.id.desc()).all()
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
    stats = {
        'toys_count': Toy.query.count(),
        'orders_count': Order.query.count(),
        'total_value': db.session.query(db.func.sum(Toy.price)).scalar() or 0,
        'orders_sum': db.session.query(db.func.sum(Order.total)).scalar() or 0,
    }
    recent_orders = Order.query.order_by(Order.id.desc()).limit(5).all()
    return render_template('admin/dashboard.html', stats=stats, recent_orders=recent_orders)


@app.route('/admin/products')
@admin_required
def admin_products():
    toys = Toy.query.order_by(Toy.id).all()
    return render_template('admin/products.html', toys=toys)


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
        flash(f'Товар "{name}" удалён', 'success')
    else:
        flash('Товар не найден', 'error')
    return redirect(url_for('admin_products'))


@app.route('/admin/orders')
@admin_required
def admin_orders():
    orders = Order.query.order_by(Order.id.desc()).all()
    return render_template('admin/orders.html', orders=orders)


@app.route('/admin/orders/<int:order_id>')
@admin_required
def admin_order_detail(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        flash('Заказ не найден', 'error')
        return redirect(url_for('admin_orders'))
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

    order.status = new_status
    db.session.commit()
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


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', 5050))
    app.run(debug=debug_mode, host=host, port=port)