// ===== Кнопки +/- для количества =====
document.addEventListener('click', function (e) {
    const btn = e.target.closest('.quantity__btn');
    if (!btn) return;

    const wrapper = btn.closest('.quantity');
    if (!wrapper) return;

    const input = wrapper.querySelector('.quantity__input');
    if (!input) return;

    const action = btn.dataset.action;
    let value = parseInt(input.value, 10);
    if (Number.isNaN(value)) value = parseInt(input.min, 10) || 1;

    const min = parseInt(input.min, 10) || 1;
    const max = parseInt(input.max, 10) || 99;

    if (action === 'plus' && value < max) value++;
    if (action === 'minus' && value > min) value--;
    if (action === 'minus' && value < min) value = min;

    input.value = value;

    const form = input.closest('form');
    if (form && form.classList.contains('quantity-form')) {
        form.submit();
    }
});

// ===== Подтверждение через data-confirm =====
document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!form.dataset || !form.dataset.confirm) return;

    if (!window.confirm(form.dataset.confirm)) {
        e.preventDefault();
        e.stopImmediatePropagation();
    }
}, true);

// ===== Открытие/закрытие модалок (замена инлайн onclick) =====
document.addEventListener('click', function (e) {
    const opener = e.target.closest('[data-modal-open]');
    if (opener) {
        const modalId = opener.dataset.modalOpen;
        const modal = document.getElementById(modalId);
        if (modal) modal.style.display = 'flex';
        return;
    }

    const closer = e.target.closest('[data-modal-close]');
    if (closer) {
        const modalId = closer.dataset.modalClose;
        const modal = document.getElementById(modalId);
        if (modal) modal.style.display = 'none';
    }
});

// ===== Копирование ссылки на заказ =====
document.addEventListener('click', function (e) {
    const btn = e.target.closest('#copy-order-link-btn');
    if (!btn) return;

    const input = document.getElementById('order-link-input');
    if (!input) return;

    input.select();
    input.setSelectionRange(0, 99999);

    const done = () => {
        if (typeof showToast === 'function') {
            showToast('Ссылка скопирована!', 'success');
        }
    };

    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(input.value).then(done).catch(() => {
            try { document.execCommand('copy'); done(); } catch (err) {}
        });
    } else {
        try { document.execCommand('copy'); done(); } catch (err) {}
    }
});

// ===== Хелпер: CSRF-токен =====
function getCsrfToken(form) {
    if (form) {
        const input = form.querySelector('input[name="csrf_token"]');
        if (input && input.value) return input.value;
    }
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
}

// ===== AJAX добавление в корзину =====
document.addEventListener('submit', async function (e) {
    const form = e.target;
    if (!form.matches('.add-form')) return;

    e.preventDefault();

    const btn = form.querySelector('button[type="submit"]');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '...';

    const csrfToken = getCsrfToken(form);

    try {
        const response = await fetch(form.action, {
            method: 'POST',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRFToken': csrfToken
            },
            body: new FormData(form),
            credentials: 'same-origin'
        });

        if (!response.ok) {
            throw new Error('HTTP ' + response.status);
        }

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
        console.error('[add_to_cart] AJAX упал, обычная отправка:', err);
        btn.disabled = false;
        btn.textContent = originalText;
        HTMLFormElement.prototype.submit.call(form);
    }
});

// ===== Уведомления =====
function showToast(message, type = 'success') {
    const toast = document.createElement('div');
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

// ===== Плавное появление карточек =====
document.addEventListener('DOMContentLoaded', function () {
    const cards = document.querySelectorAll('.product-card');

    if (!('IntersectionObserver' in window)) {
        cards.forEach(card => {
            card.style.opacity = '1';
            card.style.transform = 'translateY(0)';
        });
        return;
    }

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