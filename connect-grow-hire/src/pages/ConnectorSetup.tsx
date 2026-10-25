import { useState, useEffect } from 'react';
import { Helmet } from 'react-helmet-async';
import { useNavigate, Link } from 'react-router-dom';
import { Menu, X, ArrowRight, Copy, Check, Search, Building2, Mail } from 'lucide-react';
import OfferloopLogo from '@/assets/offerloop_logo2.png';

// Visual system tokens — sampled to match /about and /for-students so this
// page reads as the same surface.
const C = {
  pageBg: '#F5F6F8',
  ink: '#003262',
  inkSubtle: '#4D619F',
  brand: '#2563EB',
  primaryBtn: '#4C62A8',
  primaryBtnHover: '#3D5293',
  body: '#475569',
  muted: '#64748B',
  cardBg: '#F7F9FE',
  cardBorder: '#E2E8F0',
  eyebrow: '#6478B4',
  divider: '#EEF2F8',
  navBlue: '#4A5E80',
};

const CONNECTOR_URL = 'https://www.offerloop.ai/mcp';

const Highlight: React.FC<React.PropsWithChildren> = ({ children }) => (
  <span style={{ fontStyle: 'italic', color: C.inkSubtle }}>{children}</span>
);

// The connector setup sub-steps, shown as a checklist beneath the video.
const connectSteps: { title: string; body: React.ReactNode }[] = [
  {
    title: 'Open Connectors in Claude',
    body: (
      <>
        In Claude Desktop or claude.ai, go to{' '}
        <strong>Settings → Connectors → Add custom connector</strong>.
      </>
    ),
  },
  { title: 'Paste the Offerloop URL', body: 'Drop in the connector URL above and confirm.' },
  {
    title: 'Sign in to Offerloop',
    body: 'A browser tab opens — sign in with the same Google account from Step 1.',
  },
  { title: 'Click Allow', body: 'Grant Claude access to run searches on your behalf.' },
  {
    title: 'You’re connected',
    body: (
      <>
        Claude shows <strong>Offerloop connected</strong>. That’s it.
      </>
    ),
  },
];

const examples = [
  { icon: Search, label: 'Find people', text: '"Find people at McKinsey in the Chicago office."' },
  { icon: Building2, label: 'Company intel', text: '"Give me an overview of Jane Street and how many alumni from my school work there."' },
  { icon: Mail, label: 'Draft outreach', text: '"Draft a cold email to this analyst at Goldman."' },
];

const ConnectorSetup = () => {
  const navigate = useNavigate();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [navbarScrolled, setNavbarScrolled] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const handleScroll = () => setNavbarScrolled(window.scrollY > 50);
    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) entry.target.classList.add('visible');
        });
      },
      { threshold: 0.1, rootMargin: '0px 0px -30px 0px' },
    );
    const elements = document.querySelectorAll('.reveal');
    elements.forEach((el) => observer.observe(el));
    return () => elements.forEach((el) => observer.unobserve(el));
  }, []);

  const copyUrl = async () => {
    try {
      await navigator.clipboard.writeText(CONNECTOR_URL);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard unavailable — user can still select the text */
    }
  };

  const goSignup = () => navigate('/signin?mode=signup');

  return (
    <div
      className="min-h-screen w-full"
      style={{ fontFamily: "'Inter', sans-serif", background: C.pageBg, color: C.ink }}
    >
      <Helmet>
        <title>Connect Offerloop to Claude — Setup Guide</title>
        <meta
          name="description"
          content="Connect Offerloop to Claude in under two minutes. Create an account, add the custom connector, and let Claude find contacts, pull company intel, and draft outreach on your behalf."
        />
        <link rel="canonical" href="https://offerloop.ai/connector" />
        <meta property="og:title" content="Connect Offerloop to Claude — Setup Guide" />
        <meta property="og:description" content="Add Offerloop as a custom connector in Claude. Results respect your Offerloop tier — Pro gives 10 per search, Elite gives 15." />
        <meta property="og:url" content="https://offerloop.ai/connector" />
        <meta property="og:type" content="website" />
      </Helmet>

      {/* ═══════════════ PAGE-SCOPED STYLES ═══════════════ */}
      <style>{`
        .cn-nav-link {
          position: relative;
          font-family: 'Libre Baskerville', Georgia, serif;
          font-size: 13px;
          font-weight: 600;
          text-decoration: none;
          color: ${C.navBlue};
          padding: 4px 2px;
          transition: color 0.15s ease;
        }
        .cn-nav-link::after {
          content: '';
          position: absolute;
          left: 0; right: 0; bottom: -2px;
          height: 2px;
          background: ${C.brand};
          transform: scaleX(0);
          transform-origin: center;
          transition: transform 0.2s ease;
        }
        .cn-nav-link:hover { color: #0F172A; }
        .cn-nav-link:hover::after { transform: scaleX(0.5); }

        .cn-btn-primary {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          background: ${C.primaryBtn};
          color: #fff;
          font-family: 'Inter', sans-serif;
          font-size: 16px;
          font-weight: 600;
          padding: 14px 28px;
          border-radius: 10px;
          border: none;
          cursor: pointer;
          text-decoration: none;
          box-shadow: 0 1px 1px rgba(0,0,0,0.1), 0 6px 18px rgba(76, 98, 168, 0.18);
          transition: background 0.18s ease, transform 0.18s ease, box-shadow 0.18s ease;
        }
        .cn-btn-primary:hover {
          background: ${C.primaryBtnHover};
          transform: translateY(-1px);
          box-shadow: 0 1px 1px rgba(0,0,0,0.1), 0 10px 24px rgba(76, 98, 168, 0.24);
        }

        .cn-card {
          background: #ffffff;
          border: 1px solid ${C.cardBorder};
          border-radius: 14px;
          box-shadow:
            0 1px 2px rgba(15,37,69,.04),
            0 12px 22px -8px rgba(15,37,69,.10),
            0 4px 8px -4px rgba(15,37,69,.06);
        }

        .reveal { opacity: 0; transform: translateY(16px); transition: opacity .55s ease, transform .55s ease; }
        .reveal.visible { opacity: 1; transform: translateY(0); }

        .cn-video {
          display: block;
          width: 100%;
          border-radius: 14px;
          background: #0F172A;
          box-shadow:
            0 1px 2px rgba(15,37,69,.06),
            0 24px 48px -16px rgba(15,37,69,.28),
            0 8px 16px -8px rgba(15,37,69,.12);
        }

        .cn-media-slot {
          aspect-ratio: 16 / 9;
          width: 100%;
          border-radius: 14px;
          border: 1.5px dashed ${C.cardBorder};
          background:
            radial-gradient(rgba(15, 37, 69, 0.06) 1.2px, transparent 1.4px);
          background-size: 18px 18px;
          background-color: #FBFCFE;
          display: flex;
          flex-direction: column;
          gap: 4px;
          align-items: center;
          justify-content: center;
          color: ${C.muted};
          font-size: 14px;
          font-weight: 600;
          text-align: center;
          padding: 24px;
        }

        .cn-step-pill {
          display: inline-flex;
          align-items: center;
          gap: 10px;
          font-family: 'Inter', sans-serif;
          font-size: 13px;
          font-weight: 700;
          letter-spacing: 0.14em;
          text-transform: uppercase;
          color: ${C.brand};
          margin: 0 0 16px;
        }
        .cn-step-num {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 26px;
          height: 26px;
          border-radius: 50%;
          background: ${C.brand};
          color: #fff;
          font-family: 'Libre Baskerville', Georgia, serif;
          font-size: 14px;
          letter-spacing: 0;
        }
      `}</style>

      {/* ═══════════════ NAVBAR ═══════════════ */}
      <header
        className="fixed top-0 left-0 right-0 z-50 h-14 flex items-center justify-between px-6 md:px-12"
        style={{
          background: navbarScrolled ? 'rgba(255,255,255,0.96)' : 'rgba(255,255,255,0.88)',
          backdropFilter: 'blur(16px) saturate(1.4)',
          WebkitBackdropFilter: 'blur(16px) saturate(1.4)',
          borderBottom: `1px solid ${navbarScrolled ? 'rgba(214, 222, 240, 0.8)' : 'rgba(214, 222, 240, 0.6)'}`,
          transition: 'all 0.3s ease',
        }}
      >
        <div className="flex items-center">
          <img src={OfferloopLogo} alt="Offerloop" className="h-16 cursor-pointer" onClick={() => navigate('/')} />
        </div>

        <nav
          className="hidden md:flex items-center"
          style={{ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%, -50%)', gap: 32 }}
        >
          <Link to="/for-students" className="cn-nav-link">For Students</Link>
          <Link to="/pricing" className="cn-nav-link">Pricing</Link>
          <Link to="/about" className="cn-nav-link">About</Link>
        </nav>

        <div className="hidden md:flex items-center" style={{ marginLeft: 'auto', flexShrink: 0 }}>
          <button
            onClick={() => navigate('/onboarding')}
            style={{
              background: C.brand,
              color: '#fff',
              fontSize: 13,
              fontWeight: 600,
              fontFamily: "'Libre Baskerville', Georgia, serif",
              padding: '8px 20px',
              borderRadius: 100,
              border: 'none',
              cursor: 'pointer',
              transition: 'background 0.15s ease, transform 0.15s ease',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = '#1D4ED8';
              e.currentTarget.style.transform = 'translateY(-1px)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = C.brand;
              e.currentTarget.style.transform = 'translateY(0)';
            }}
          >
            Create account
          </button>
        </div>

        <button onClick={() => setMobileMenuOpen(!mobileMenuOpen)} className="md:hidden p-2" style={{ color: C.navBlue }}>
          {mobileMenuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </button>
      </header>

      {/* Mobile Menu */}
      {mobileMenuOpen && (
        <div
          className="fixed top-[72px] left-4 right-4 md:hidden z-40"
          style={{
            background: 'rgba(255,255,255,0.98)',
            border: '1px solid rgba(37,99,235,0.1)',
            borderRadius: 16,
            boxShadow: '0 4px 24px rgba(37,99,235,0.08)',
            backdropFilter: 'blur(16px)',
          }}
        >
          <nav className="flex flex-col p-3 gap-1">
            {[
              { label: 'For Students', path: '/for-students' },
              { label: 'Pricing', path: '/pricing' },
              { label: 'About', path: '/about' },
            ].map((l) => (
              <Link
                key={l.path}
                to={l.path}
                onClick={() => setMobileMenuOpen(false)}
                className="text-left px-4 py-3 text-sm font-medium rounded-lg hover:bg-gray-50"
                style={{ color: C.navBlue, fontFamily: "'Libre Baskerville', Georgia, serif", textDecoration: 'none' }}
              >
                {l.label}
              </Link>
            ))}
            <div className="border-t mt-2 pt-2" style={{ borderColor: 'rgba(37,99,235,0.08)' }}>
              <button
                onClick={() => { navigate('/onboarding'); setMobileMenuOpen(false); }}
                className="w-full text-center py-3 text-sm font-semibold"
                style={{ background: C.brand, color: '#fff', borderRadius: 100, fontFamily: "'Libre Baskerville', Georgia, serif" }}
              >
                Create account
              </button>
            </div>
          </nav>
        </div>
      )}

      <div className="h-14" />

      {/* ═══════════════ HERO ═══════════════ */}
      <section style={{ position: 'relative', padding: '88px 32px 48px', zIndex: 1 }}>
        <div className="reveal" style={{ position: 'relative', zIndex: 2, maxWidth: 820, margin: '0 auto', textAlign: 'center' }}>
          <h1
            style={{
              fontFamily: "'Libre Baskerville', Georgia, serif",
              fontSize: 'clamp(38px, 5vw, 56px)',
              fontWeight: 400,
              lineHeight: 1.1,
              letterSpacing: '-0.015em',
              color: C.ink,
              margin: '0 0 20px',
            }}
          >
            Connect Offerloop to <Highlight>Claude</Highlight>
          </h1>
          <p
            style={{
              fontFamily: "'Inter', sans-serif",
              fontSize: 'clamp(16px, 1.4vw, 19px)',
              lineHeight: 1.7,
              color: C.body,
              letterSpacing: '0.02em',
              margin: '0 auto',
              maxWidth: 640,
            }}
          >
            Two steps, under two minutes. Create your account, add the connector, and
            Claude can find contacts, pull company intel, and draft outreach for you,
            with results that respect your actual Offerloop tier.
          </p>
        </div>
      </section>

      {/* ═══════════════ STEP 1 — CREATE ACCOUNT ═══════════════ */}
      <section style={{ padding: '24px 32px 16px', position: 'relative', zIndex: 1 }}>
        <div className="reveal cn-card" style={{ maxWidth: 820, margin: '0 auto', padding: '36px 40px' }}>
          <p className="cn-step-pill"><span className="cn-step-num">1</span> Create your account</p>
          <h2
            style={{
              fontFamily: "'Libre Baskerville', Georgia, serif",
              fontSize: 'clamp(24px, 3vw, 30px)',
              fontWeight: 400,
              lineHeight: 1.2,
              color: C.ink,
              margin: '0 0 14px',
            }}
          >
            Sign up at Offerloop
          </h2>
          <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 17, lineHeight: 1.7, color: C.body, margin: '0 0 24px', maxWidth: 600 }}>
            Make an account with your Google account. This is the account Claude
            connects to, and your tier sets how many results come back per search —
            so sign in with the same Google account in the next step.
          </p>
          <button onClick={goSignup} className="cn-btn-primary">
            Create free account
            <ArrowRight size={16} strokeWidth={2.3} />
          </button>
        </div>
      </section>

      {/* ═══════════════ STEP 2 — CONNECT (VIDEO FRONT & CENTER) ═══════════════ */}
      <section style={{ padding: '32px 32px 16px', position: 'relative', zIndex: 1 }}>
        <div className="reveal" style={{ maxWidth: 900, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 28 }}>
            <p className="cn-step-pill" style={{ justifyContent: 'center' }}>
              <span className="cn-step-num">2</span> Connect to Claude
            </p>
            <h2
              style={{
                fontFamily: "'Libre Baskerville', Georgia, serif",
                fontSize: 'clamp(28px, 4vw, 40px)',
                fontWeight: 400,
                lineHeight: 1.15,
                color: C.ink,
                margin: 0,
              }}
            >
              Watch the <Highlight>full walkthrough</Highlight>
            </h2>
          </div>

          {/* The walkthrough video, front and center */}
          <video
            className="cn-video"
            controls
            playsInline
            preload="metadata"
            poster="/logo.png"
          >
            <source src="/connector-setup.mp4" type="video/mp4" />
            Your browser doesn’t support embedded video. {' '}
            <a href="/connector-setup.mp4">Download the walkthrough</a>.
          </video>
        </div>
      </section>

      {/* Connector URL + written checklist */}
      <section style={{ padding: '28px 32px 64px', position: 'relative', zIndex: 1 }}>
        <div className="reveal cn-card" style={{ maxWidth: 820, margin: '0 auto', padding: '32px 40px' }}>
          {/* URL row */}
          <p
            style={{
              fontFamily: "'Inter', sans-serif",
              fontSize: 12,
              fontWeight: 700,
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color: C.eyebrow,
              margin: '0 0 12px',
            }}
          >
            Connector URL
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 32 }}>
            <code
              style={{
                flex: '1 1 280px',
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: 16,
                color: C.ink,
                background: '#F1F5FB',
                border: `1px solid ${C.cardBorder}`,
                borderRadius: 8,
                padding: '12px 16px',
                wordBreak: 'break-all',
              }}
            >
              {CONNECTOR_URL}
            </code>
            <button onClick={copyUrl} className="cn-btn-primary" style={{ padding: '12px 20px', fontSize: 14 }}>
              {copied ? <Check size={16} strokeWidth={2.4} /> : <Copy size={16} strokeWidth={2.2} />}
              {copied ? 'Copied' : 'Copy URL'}
            </button>
          </div>

          {/* Checklist */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
            {connectSteps.map((step, i) => (
              <div key={i} style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
                <div
                  style={{
                    flexShrink: 0,
                    width: 28,
                    height: 28,
                    borderRadius: '50%',
                    background: '#EEF3FD',
                    color: C.brand,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontFamily: "'Libre Baskerville', Georgia, serif",
                    fontSize: 14,
                    fontWeight: 700,
                  }}
                >
                  {i + 1}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 16, fontWeight: 700, color: C.ink, margin: '3px 0 2px' }}>
                    {step.title}
                  </p>
                  <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 15, lineHeight: 1.6, color: C.body, margin: 0 }}>
                    {step.body}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ═══════════════ SEE IT IN ACTION ═══════════════ */}
      <section style={{ padding: '64px 32px 88px', position: 'relative', zIndex: 1, background: '#FBFCFE', borderTop: `1px solid ${C.divider}`, borderBottom: `1px solid ${C.divider}` }}>
        <div className="reveal" style={{ textAlign: 'center', maxWidth: 760, margin: '0 auto 36px' }}>
          <p
            style={{
              fontFamily: "'Inter', sans-serif",
              fontSize: 14,
              fontWeight: 700,
              letterSpacing: '0.18em',
              color: C.eyebrow,
              textTransform: 'uppercase',
              margin: '0 0 14px',
            }}
          >
            Once connected
          </p>
          <h2
            style={{
              fontFamily: "'Libre Baskerville', Georgia, serif",
              fontSize: 'clamp(28px, 4vw, 40px)',
              fontWeight: 400,
              lineHeight: 1.15,
              color: C.ink,
              margin: '0 0 16px',
            }}
          >
            See it <Highlight>in action</Highlight>
          </h2>
          <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 17, lineHeight: 1.65, color: C.body, margin: '0 auto', maxWidth: 560 }}>
            Just ask Claude. Pro tier returns 10 contacts per search, Elite returns 15.
          </p>
        </div>

        {/* Slot for the Claude usage video (coming soon) */}
        <div className="reveal" style={{ maxWidth: 900, margin: '0 auto 40px' }}>
          <div className="cn-media-slot">
            <span style={{ fontSize: 15 }}>Demo video coming soon</span>
            <span style={{ fontSize: 13, fontWeight: 500, color: C.muted }}>Offerloop running inside Claude</span>
          </div>
        </div>

        <div
          className="reveal"
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
            gap: 22,
            maxWidth: 980,
            margin: '0 auto',
          }}
        >
          {examples.map(({ icon: Icon, label, text }) => (
            <div key={label} className="cn-card" style={{ padding: '26px 24px' }}>
              <div
                style={{
                  width: 40,
                  height: 40,
                  borderRadius: 10,
                  background: '#EEF3FD',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  marginBottom: 16,
                }}
              >
                <Icon size={20} strokeWidth={2} color={C.brand} />
              </div>
              <p
                style={{
                  fontFamily: "'Inter', sans-serif",
                  fontSize: 12,
                  fontWeight: 700,
                  letterSpacing: '0.14em',
                  textTransform: 'uppercase',
                  color: C.eyebrow,
                  margin: '0 0 8px',
                }}
              >
                {label}
              </p>
              <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 15, lineHeight: 1.6, color: C.body, margin: 0, fontStyle: 'italic' }}>
                {text}
              </p>
            </div>
          ))}
        </div>
      </section>

      {/* ═══════════════ HEADS UP ═══════════════ */}
      <section style={{ padding: '56px 32px', position: 'relative', zIndex: 1 }}>
        <div
          className="reveal"
          style={{
            maxWidth: 720,
            margin: '0 auto',
            background: '#FFF8EC',
            border: '1px solid #F2DFB8',
            borderRadius: 14,
            padding: '24px 28px',
            display: 'flex',
            gap: 16,
            alignItems: 'flex-start',
          }}
        >
          <span style={{ fontSize: 22, lineHeight: 1 }} aria-hidden>⚠️</span>
          <div>
            <h3 style={{ fontFamily: "'Libre Baskerville', Georgia, serif", fontSize: 18, fontWeight: 400, color: '#7C5A12', margin: '2px 0 8px' }}>
              Gmail drafts aren&apos;t wired up yet
            </h3>
            <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 15, lineHeight: 1.6, color: '#8A6A2E', margin: 0 }}>
              If you ask Claude to draft outreach, you&apos;ll get the text back in the
              chat — but it won&apos;t land in your Gmail drafts yet. That integration
              is next on the list.
            </p>
          </div>
        </div>
      </section>

      {/* ═══════════════ FINAL CTA ═══════════════ */}
      <section style={{ padding: '64px 32px 110px', textAlign: 'center', position: 'relative', zIndex: 1 }}>
        <div style={{ maxWidth: 640, margin: '0 auto' }}>
          <h2
            className="reveal"
            style={{
              fontFamily: "'Libre Baskerville', Georgia, serif",
              fontSize: 'clamp(30px, 4.4vw, 44px)',
              fontWeight: 400,
              lineHeight: 1.1,
              letterSpacing: '-0.015em',
              color: C.ink,
              margin: '0 0 18px',
            }}
          >
            Run it the right <Highlight>way</Highlight>
          </h2>
          <p
            className="reveal"
            style={{
              fontFamily: "'Inter', sans-serif",
              fontSize: 17,
              lineHeight: 1.65,
              color: C.body,
              letterSpacing: '0.02em',
              margin: '0 auto 32px',
              maxWidth: 520,
            }}
          >
            Your tier sets how many contacts come back per search. Upgrade to Pro for
            8, Elite for 15.
          </p>
          <button onClick={() => navigate('/pricing')} className="cn-btn-primary reveal">
            See plans
            <ArrowRight size={16} strokeWidth={2.3} />
          </button>
        </div>
      </section>

      {/* ═══════════════ FOOTER ═══════════════ */}
      <footer style={{ background: '#0F172A', color: '#94A3B8', padding: '56px 32px 32px' }}>
        <div
          style={{
            maxWidth: 1200,
            margin: '0 auto',
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            gap: 36,
          }}
        >
          <div>
            <p style={{ fontFamily: "'Lora', Georgia, serif", fontSize: 22, color: '#fff', margin: '0 0 12px', letterSpacing: '-0.01em' }}>
              Offerloop
            </p>
            <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 13, lineHeight: 1.6, color: '#94A3B8', margin: 0, maxWidth: 260 }}>
              Built by students, for students. Networking, automated.
            </p>
          </div>

          <div>
            <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 12, fontWeight: 700, letterSpacing: '0.16em', color: '#E2E8F0', textTransform: 'uppercase', margin: '0 0 14px' }}>
              Company
            </p>
            {[
              { label: 'About', path: '/about' },
              { label: 'Pricing', path: '/pricing' },
              { label: 'Contact', path: '/contact-us' },
            ].map((l) => (
              <Link
                key={l.path}
                to={l.path}
                style={{ display: 'block', fontFamily: "'Inter', sans-serif", fontSize: 13, color: '#94A3B8', textDecoration: 'none', margin: '0 0 10px', transition: 'color 0.15s ease' }}
                onMouseEnter={(e) => { e.currentTarget.style.color = '#fff'; }}
                onMouseLeave={(e) => { e.currentTarget.style.color = '#94A3B8'; }}
              >
                {l.label}
              </Link>
            ))}
          </div>

          <div>
            <p style={{ fontFamily: "'Inter', sans-serif", fontSize: 12, fontWeight: 700, letterSpacing: '0.16em', color: '#E2E8F0', textTransform: 'uppercase', margin: '0 0 14px' }}>
              Resources
            </p>
            {[
              { label: 'For Students', path: '/for-students' },
              { label: 'Privacy', path: '/privacy' },
              { label: 'Terms', path: '/terms-of-service' },
            ].map((l) => (
              <Link
                key={l.path}
                to={l.path}
                style={{ display: 'block', fontFamily: "'Inter', sans-serif", fontSize: 13, color: '#94A3B8', textDecoration: 'none', margin: '0 0 10px', transition: 'color 0.15s ease' }}
                onMouseEnter={(e) => { e.currentTarget.style.color = '#fff'; }}
                onMouseLeave={(e) => { e.currentTarget.style.color = '#94A3B8'; }}
              >
                {l.label}
              </Link>
            ))}
          </div>
        </div>

        <div
          style={{
            maxWidth: 1200,
            margin: '40px auto 0',
            paddingTop: 24,
            borderTop: '1px solid rgba(148, 163, 184, 0.18)',
            fontFamily: "'Inter', sans-serif",
            fontSize: 12,
            color: '#64748B',
          }}
        >
          © 2026 Offerloop. All rights reserved.
        </div>
      </footer>
    </div>
  );
};

export default ConnectorSetup;
