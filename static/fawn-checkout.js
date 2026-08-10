/*!
 * FAWN Checkout SDK v0.1 — drop-in payment widget for merchants.
 *
 * Integration is deliberately one <script> tag and one function call, so a
 * dispensary's existing web/POS developer can ship it in well under a day.
 *
 *   <script src="https://web-production-13d5b.up.railway.app/static/fawn-checkout.js"></script>
 *   <div id="pay"></div>
 *   <script>
 *     FAWN.mount('#pay', {
 *       checkoutToken: 'chk_...',        // created server-side, see below
 *       onPaid:   r => console.log('paid', r),
 *       onCancel: () => console.log('cancelled')
 *     });
 *   </script>
 *
 * SECURITY MODEL — read this before integrating
 * ---------------------------------------------
 * This file runs in the CUSTOMER's browser, so it never sees your API key.
 * Your server creates the checkout with your secret key and passes only the
 * resulting short-lived `checkout_token` to the page:
 *
 *   curl -X POST https://…/closed-loop/merchant/checkouts \
 *        -H "X-FAWN-Key: fawn_sk_live_…" \
 *        -d '{"amount_cents": 4500, "order_reference": "TICKET-1234"}'
 *
 * Never put fawn_sk_* in front-end code. A leaked secret key lets anyone
 * create charges against your account.
 *
 * The widget polls public checkout status; it does not itself move money.
 * Settlement is authorized by the customer inside the FAWN app (or by an
 * in-store NFC tap), never by this script.
 */
(function (global) {
  'use strict';

  var DEFAULT_API = 'https://web-production-13d5b.up.railway.app';
  var POLL_MS = 2000;
  var POLL_TIMEOUT_MS = 15 * 60 * 1000; // stop after 15 min; checkouts expire

  function fmt(cents) {
    return '$' + (Number(cents || 0) / 100).toFixed(2);
  }

  function el(tag, css, text) {
    var n = document.createElement(tag);
    if (css) n.style.cssText = css;
    if (text != null) n.textContent = text; // textContent, never innerHTML
    return n;
  }

  function styles() {
    if (document.getElementById('fawn-checkout-styles')) return;
    var s = document.createElement('style');
    s.id = 'fawn-checkout-styles';
    s.textContent = [
      '.fawn-co{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;',
      'border:1px solid #2a2a2a;border-radius:16px;padding:20px;background:#161616;color:#f0f0f0;max-width:360px}',
      '.fawn-co__amt{font-size:2rem;font-weight:800;letter-spacing:-1px;margin:4px 0 2px}',
      '.fawn-co__fee{font-size:.75rem;color:#888;margin-bottom:14px}',
      '.fawn-co__qr{background:#fff;padding:12px;border-radius:12px;display:inline-block;margin-bottom:12px}',
      '.fawn-co__btn{display:block;width:100%;padding:13px;background:#00c896;color:#000;font-weight:800;',
      'border:none;border-radius:10px;cursor:pointer;font-size:.95rem;text-align:center;text-decoration:none}',
      '.fawn-co__btn:disabled{opacity:.55;cursor:not-allowed}',
      '.fawn-co__status{font-size:.82rem;color:#888;margin-top:10px;min-height:18px}',
      '.fawn-co__ok{color:#00c896;font-weight:700}',
      '.fawn-co__err{color:#ff4d4d;font-weight:700}',
      '@media(prefers-color-scheme:light){.fawn-co{background:#fff;color:#1a1a1a;border-color:#e0e3e8}}'
    ].join('');
    document.head.appendChild(s);
  }

  function FawnCheckout(target, opts) {
    var node = typeof target === 'string' ? document.querySelector(target) : target;
    if (!node) throw new Error('FAWN.mount: target element not found');
    if (!opts || !opts.checkoutToken) throw new Error('FAWN.mount: checkoutToken is required');

    var api = (opts.apiBase || DEFAULT_API).replace(/\/$/, '');
    var token = opts.checkoutToken;
    var stopped = false;
    var timer = null;
    var started = Date.now();

    styles();
    node.innerHTML = '';
    var card = el('div');
    card.className = 'fawn-co';
    var amount = el('div', null, '—'); amount.className = 'fawn-co__amt';
    var fee = el('div', null, ''); fee.className = 'fawn-co__fee';
    var qrWrap = el('div'); qrWrap.className = 'fawn-co__qr'; qrWrap.style.display = 'none';
    var payBtn = el('a', null, 'Pay with FAWN'); payBtn.className = 'fawn-co__btn';
    var status = el('div', null, 'Loading…'); status.className = 'fawn-co__status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    card.appendChild(el('div', 'font-size:.7rem;text-transform:uppercase;letter-spacing:1px;color:#888', 'Pay with FAWN'));
    card.appendChild(amount); card.appendChild(fee);
    card.appendChild(qrWrap); card.appendChild(payBtn); card.appendChild(status);
    node.appendChild(card);

    function set(msg, cls) {
      status.textContent = msg;
      status.className = 'fawn-co__status' + (cls ? ' ' + cls : '');
    }

    function stop() {
      stopped = true;
      if (timer) { clearTimeout(timer); timer = null; }
    }

    function renderQr(url) {
      // Uses the browser-native QR path when available; otherwise the deep
      // link button alone is sufficient (mobile users tap it directly).
      qrWrap.innerHTML = '';
      if (global.qrcode) {
        try {
          var qr = global.qrcode(0, 'M');
          qr.addData(url);
          qr.make();
          qrWrap.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 8 });
          qrWrap.style.display = 'inline-block';
          return;
        } catch (e) { /* fall through to link-only */ }
      }
    }

    function poll() {
      if (stopped) return;
      if (Date.now() - started > POLL_TIMEOUT_MS) {
        set('This checkout expired. Start a new one.', 'fawn-co__err');
        return stop();
      }
      fetch(api + '/closed-loop/checkouts/' + encodeURIComponent(token), {
        headers: { 'Accept': 'application/json' }
      })
        .then(function (r) {
          // 4xx is terminal (bad/unknown/expired token) — retrying it forever
          // would spin silently. 5xx and network faults are transient.
          if (r.status >= 400 && r.status < 500) {
            var fatal = new Error('checkout_not_found');
            fatal.terminal = true;
            fatal.httpStatus = r.status;
            throw fatal;
          }
          if (!r.ok) throw new Error('status ' + r.status);
          return r.json();
        })
        .then(function (d) {
          amount.textContent = fmt(d.amount_cents);
          var payer = d.payer_total_cents != null ? d.payer_total_cents : d.amount_cents;
          fee.textContent = 'Customer pays ' + fmt(payer) + ' (includes $0.01 FAWN fee)';

          var deepLink = d.checkout_url || (api + '/closed-loop/checkouts/' + token);
          payBtn.setAttribute('href', deepLink);
          payBtn.setAttribute('target', '_blank');
          payBtn.setAttribute('rel', 'noopener');
          renderQr(deepLink);

          if (d.status === 'completed') {
            set('Paid — thank you!', 'fawn-co__ok');
            payBtn.style.display = 'none';
            qrWrap.style.display = 'none';
            stop();
            if (typeof opts.onPaid === 'function') opts.onPaid(d);
            return;
          }
          if (d.status === 'cancelled' || d.status === 'expired') {
            set('Checkout ' + d.status + '.', 'fawn-co__err');
            payBtn.style.display = 'none';
            stop();
            if (typeof opts.onCancel === 'function') opts.onCancel(d);
            return;
          }
          set('Waiting for customer to approve in the FAWN app…');
          timer = setTimeout(poll, POLL_MS);
        })
        .catch(function (err) {
          if (err && err.terminal) {
            // Unknown/expired checkout token — stop, don't spin forever.
            set('This checkout is no longer valid. Ask staff for a new one.', 'fawn-co__err');
            payBtn.style.display = 'none';
            qrWrap.style.display = 'none';
            stop();
            if (typeof opts.onError === 'function') opts.onError(err);
            return;
          }
          // Transient network failures must not kill the widget mid-sale.
          set('Reconnecting…');
          timer = setTimeout(poll, POLL_MS * 2);
          if (typeof opts.onError === 'function') opts.onError(err);
        });
    }

    poll();
    return { destroy: function () { stop(); node.innerHTML = ''; } };
  }

  global.FAWN = global.FAWN || {};
  global.FAWN.mount = FawnCheckout;
  global.FAWN.version = '0.1.0';
})(window);
