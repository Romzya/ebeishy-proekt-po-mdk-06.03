// ===== Кнопки +/- для количества =====
document.addEventListener('click', function (e) {
    const btn = e.target.closest('.quantity__btn');
    if (!btn) return;

    const wrapper = btn.closest('.quantity');
    if (!wrapper) return;

    const input = wrapper.querySelector('.quantity__input');
    if (!input) return;

    const action = btn.dataset.action;
    let value = parseInt(input.value) || 1;
    const min = parseInt(input.min) || 1;
    const max = parseInt(input.max) || 99;

    if (action === 'plus' && value < max) value++;
    if (action === 'minus' && value > min) value--;
    if (action === 'minus' && value < min) value = min;

    input.value = value;

    const form = input.closest('form');
    if (form && form.classList.contains('quantity-form')) {
        form.submit();
    }
});

// ===== AJAX добавление в корзину =====
document.addEventListener('submit', async function (e) {
    const form = e.target;
    if (!form.matches('.add-form')) return;

    e.preventDefault();

    const btn = form.querySelector('button[type="submit"]');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '...';

    try {
        const response = await fetch(form.action, {
            method: 'POST',
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        });

        const data = await response.json();

        if (data.success) {
            const badge = document.querySelector('.cart-badge');
            const cartLink = document.querySelector('.cart-link');

            if (badge) {
                badge.textContent = data.cart_count;
            } else if (cartLink) {
                const newBadge = document.createElement('span');
                newBadge.className = 'cart-badge';
                newBadge.textContent = data.cart_count;
                cartLink.appendChild(newBadge);
            }

            showToast(data.message, 'success');
            btn.textContent = '✓ Добавлено';
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 1200);
        } else {
            showToast(data.message || 'Ошибка', 'error');
            btn.textContent = originalText;
            btn.disabled = false;
        }
    } catch (err) {
        form.submit();
    }
});

// ===== Всплывающие уведомления =====
function showToast(message, type = 'success') {
    let toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => toast.classList.add('toast--show'), 10);

    setTimeout(() => {
        toast.classList.remove('toast--show');
        setTimeout(() => toast.remove(), 300);
    }, 2500);
}

// ===== Стили для тоста =====
const toastStyle = document.createElement('style');
toastStyle.textContent = `
.toast {
    position: fixed;
    bottom: 30px;
    left: 50%;
    transform: translateX(-50%) translateY(100px);
    background: #2d3436;
    color: #fff;
    padding: 14px 24px;
    border-radius: 14px;
    font-weight: 700;
    font-size: .95rem;
    box-shadow: 0 8px 30px rgba(0,0,0,.2);
    z-index: 9999;
    opacity: 0;
    transition: all .3s ease;
    max-width: 90%;
    text-align: center;
}
.toast--show {
    transform: translateX(-50%) translateY(0);
    opacity: 1;
}
.toast--success { background: #00b894; }
.toast--error { background: #e17055; }
`;
document.head.appendChild(toastStyle);

// ===== Плавное появление карточек товаров =====
document.addEventListener('DOMContentLoaded', function () {
    const cards = document.querySelectorAll('.product-card');
    const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry, i) => {
            if (entry.isIntersecting) {
                setTimeout(() => {
                    entry.target.style.opacity = '1';
                    entry.target.style.transform = 'translateY(0)';
                }, i * 40);
                observer.unobserve(entry.target);
            }
        });
    }, { threshold: 0.05 });

    cards.forEach(card => {
        card.style.opacity = '0';
        card.style.transform = 'translateY(20px)';
        card.style.transition = 'opacity .4s ease, transform .4s ease';
        observer.observe(card);
    });
});

// ===== Маски для данных карты =====
document.addEventListener('DOMContentLoaded', function () {
    const cardNumber = document.getElementById('card_number');
    const cardExpiry = document.getElementById('card_expiry');
    const cardCvv = document.getElementById('card_cvv');

    if (cardNumber) {
        cardNumber.addEventListener('input', function (e) {
            let v = e.target.value.replace(/\D/g, '').slice(0, 16);
            e.target.value = v.replace(/(.{4})/g, '$1 ').trim();
        });
    }

    if (cardExpiry) {
        cardExpiry.addEventListener('input', function (e) {
            let v = e.target.value.replace(/\D/g, '').slice(0, 4);
            if (v.length >= 3) {
                v = v.slice(0, 2) + '/' + v.slice(2);
            }
            e.target.value = v;
        });
    }

    if (cardCvv) {
        cardCvv.addEventListener('input', function (e) {
            e.target.value = e.target.value.replace(/\D/g, '').slice(0, 3);
        });
    }
});

// ===== Закрытие модального окна по клику на фон =====
document.addEventListener('click', function (e) {
    if (e.target.classList && e.target.classList.contains('modal')) {
        e.target.style.display = 'none';
    }
});

// ===== Закрытие модалки по Escape =====
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal').forEach(m => m.style.display = 'none');
    }
});