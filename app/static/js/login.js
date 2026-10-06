(() => {
    const POLL_INTERVAL = 1500;
    const POLL_TIMEOUT = 10 * 60 * 1000;

    const button = document.getElementById('login-btn');
    const statusEl = document.getElementById('login-status');

    function setStatus(text, isError = false) {
        statusEl.textContent = text;
        statusEl.classList.toggle('error', isError);
    }

    function reset(message, isError = true) {
        button.disabled = false;
        setStatus(message, isError);
    }

    async function poll(pinId, popup) {
        const startedAt = Date.now();
        let closedSince = null;

        while (Date.now() - startedAt < POLL_TIMEOUT) {
            await new Promise(r => setTimeout(r, POLL_INTERVAL));
            let res;
            try {
                res = await fetch(`/api/auth/pin/${pinId}`);
            } catch {
                continue;
            }
            if (res.ok) {
                const data = await res.json();
                if (data.status === 'ok') {
                    if (popup && !popup.closed) popup.close();
                    setStatus('Signed in, loading…', false);
                    window.location.replace('/');
                    return;
                }
            } else {
                const data = await res.json().catch(() => ({}));
                if (popup && !popup.closed) popup.close();
                return reset(data.detail || 'Sign-in failed, please try again.');
            }

            // Give Plex a few seconds after the popup closes to register the approval.
            if (popup && popup.closed) {
                closedSince = closedSince || Date.now();
                if (Date.now() - closedSince > 6000) return reset('Sign-in window closed before approval.');
            }
        }
        reset('Sign-in timed out, please try again.');
    }

    async function startLogin() {
        button.disabled = true;
        setStatus('Waiting for Plex…', false);

        // Open the popup synchronously so the browser does not block it.
        const popup = window.open('', 'plex-auth', 'width=600,height=760');
        try {
            const res = await fetch('/api/auth/pin', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({forward_url: `${window.location.origin}/login`}),
            });
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Could not reach plex.tv');
            const pin = await res.json();

            if (popup && !popup.closed) {
                popup.location.href = pin.auth_url;
                poll(pin.id, popup);
            } else {
                // Popup blocked: use a full redirect, Plex will bring the user back to /login?pin=<id>.
                window.location.href = pin.auth_url;
            }
        } catch (e) {
            if (popup && !popup.closed) popup.close();
            reset(e.message);
        }
    }

    button.addEventListener('click', startLogin);

    const pinId = new URLSearchParams(window.location.search).get('pin');
    if (pinId) {
        if (window.opener && !window.opener.closed) {
            // We are the popup coming back from plex.tv: the main window is already polling.
            window.close();
        } else {
            button.disabled = true;
            setStatus('Finishing sign-in…', false);
            history.replaceState(null, '', '/login');
            poll(pinId, null);
        }
    }
})();
