import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Instagram, Mail, Menu, MessageCircle, MessageSquare, Sparkles, X } from "lucide-react";
import { AuthModal } from "../components/AuthModal";
import { UserMenu } from "../components/UserMenu";
import { WaitlistModal } from "../components/WaitlistModal";
import type { WaitlistSource } from "../components/WaitingListForm";
import { useAuth } from "../hooks/useAuth.tsx";
import { TRIAL_EXPIRY_DAYS } from "../constants";
import "./LandingPage.css";

// Hero section with parallax effect
interface HeroSectionProps {
    onStartTrial: () => void;
}

const HeroSection = ({ onStartTrial }: HeroSectionProps) => {
    const navigate = useNavigate();
    const heroRef = useRef<HTMLElement | null>(null);

    useEffect(() => {
        const heroEl = heroRef.current;
        if (!heroEl) return;
        const scrollContainer = heroEl.closest(".landing-page") as HTMLElement | null;

        const update = () => {
            const scrollTop = scrollContainer ? scrollContainer.scrollTop : window.scrollY;
            const heroTop = scrollContainer ? heroEl.offsetTop : heroEl.getBoundingClientRect().top + scrollTop;
            const local = Math.max(0, Math.min(scrollTop - heroTop, heroEl.offsetHeight));
            heroEl.style.setProperty("--parallax-bg", `${local * 0.14}px`);
            heroEl.style.setProperty("--parallax-fg", `${local * 0.28}px`);
        };

        update();

        const onScroll = () => update();
        if (scrollContainer) scrollContainer.addEventListener("scroll", onScroll, { passive: true });
        else window.addEventListener("scroll", onScroll, { passive: true });
        window.addEventListener("resize", onScroll);
        return () => {
            if (scrollContainer) scrollContainer.removeEventListener("scroll", onScroll);
            else window.removeEventListener("scroll", onScroll);
            window.removeEventListener("resize", onScroll);
        };
    }, []);
    return (
        <section className="landing-hero" ref={heroRef}>
            <div className="hero-bg" aria-hidden="true" />
            <div className="hero-center">
                <div className="hero-headline">
                    <h1 className="hero-title">
                        "Drop me the score. Say a few words. I'll sing it for you."
                    </h1>
                    <p className="hero-subtitle hero-subtitle-wide">
                        AI sight-singing from MusicXML, via chat. No DAW required.
                    </p>
                </div>
            </div>
            <div className="hero-footer">
                <div className="hero-actions">
                    <button
                        className="btn-primary btn-trial"
                        onClick={onStartTrial}
                    >
                        Start Free Trial <Sparkles size={18} />
                    </button>
                    <button
                        className="btn-secondary"
                        onClick={() => navigate("/demo")}
                    >
                        Try Interactive Demo <ArrowRight size={20} />
                    </button>
                </div>
            </div>
        </section>
    );
};


export default function LandingPage() {
    const navigate = useNavigate();
    const { isAuthenticated } = useAuth();
    const whatItDoesRef = useRef<HTMLElement | null>(null);
    const [openFaqIndex, setOpenFaqIndex] = useState<number | null>(0);
    const [showAuthModal, setShowAuthModal] = useState(false);
    const [showWaitlistModal, setShowWaitlistModal] = useState(false);
    const [waitlistSource, setWaitlistSource] = useState<WaitlistSource>("landing");
    const [isMenuOpen, setIsMenuOpen] = useState(false);

    const menuItems = [
        { id: "top", label: "Top" },
        { id: "what-it-does", label: "What It Does" },
        { id: "who-for", label: "Who It’s For / Not For" },
        { id: "why", label: "Why SightSinger.ai" },
        { id: "how-it-works", label: "How It Works" },
        { id: "ai-voices", label: "AI Voices" },
        { id: "pricing", label: "Pricing" },
        { id: "faq", label: "FAQ" },
        { id: "about", label: "About Me" },
    ];

    useEffect(() => {
        document.body.classList.add("landing-active");
        return () => {
            document.body.classList.remove("landing-active");
        };
    }, []);

    useEffect(() => {
        const sectionEl = whatItDoesRef.current;
        if (!sectionEl) return;
        const scrollContainer = sectionEl.closest(".landing-page") as HTMLElement | null;

        const update = () => {
            const scrollTop = scrollContainer ? scrollContainer.scrollTop : window.scrollY;
            const sectionTop = scrollContainer ? sectionEl.offsetTop : sectionEl.getBoundingClientRect().top + scrollTop;
            const local = Math.max(0, Math.min(scrollTop - sectionTop, sectionEl.offsetHeight));
            sectionEl.style.setProperty("--what-parallax-bg", `${local * 0.12}px`);
            sectionEl.style.setProperty("--what-parallax-fg", `${local * 0.22}px`);
        };

        update();

        const onScroll = () => update();
        if (scrollContainer) scrollContainer.addEventListener("scroll", onScroll, { passive: true });
        else window.addEventListener("scroll", onScroll, { passive: true });
        window.addEventListener("resize", onScroll);
        return () => {
            if (scrollContainer) scrollContainer.removeEventListener("scroll", onScroll);
            else window.removeEventListener("scroll", onScroll);
            window.removeEventListener("resize", onScroll);
        };
    }, []);

    const scrollToTop = () => {
        const container = document.querySelector(".landing-page");
        if (container) {
            container.scrollTo({ top: 0, behavior: "smooth" });
        }
    };

    const scrollToSection = (id: string) => {
        if (id === "top") {
            scrollToTop();
            return;
        }
        const container = document.querySelector(".landing-page");
        const target = document.getElementById(id);
        if (container && target) {
            container.scrollTo({ top: target.offsetTop - 72, behavior: "smooth" });
        } else if (target) {
            target.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    };

    const handleStartTrial = () => {
        if (isAuthenticated) {
            navigate("/app");
        } else {
            setShowAuthModal(true);
        }
    };

    const handleJoinWaitlist = (source: WaitlistSource) => {
        setWaitlistSource(source);
        setShowWaitlistModal(true);
    };

    return (
        <div className="landing-page">
            <nav className="landing-nav">
                <div className="nav-left">
                    <div className="brand" onClick={scrollToTop} style={{ cursor: 'pointer' }}>
                        <Sparkles className="brand-icon" />
                        <span>SightSinger.ai</span>
                    </div>
                    <div className="nav-shortcuts">
                        <button type="button" onClick={() => scrollToSection("what-it-does")}>
                            What it is
                        </button>
                        <button type="button" onClick={() => scrollToSection("who-for")}>
                            Who's for
                        </button>
                        <button type="button" onClick={() => scrollToSection("pricing")}>
                            Pricing
                        </button>
                    </div>
                </div>
                <div className="nav-menu">
                    <button
                        className="nav-menu-toggle"
                        onClick={() => setIsMenuOpen((prev) => !prev)}
                        aria-expanded={isMenuOpen}
                        aria-controls="landing-menu"
                    >
                        {isMenuOpen ? <X size={20} /> : <Menu size={20} />}
                    </button>
                    {isMenuOpen && (
                        <div id="landing-menu" className="nav-menu-dropdown">
                            {menuItems.map((item) => (
                                <button
                                    key={item.id}
                                    className="nav-menu-dropdown-item"
                                    onClick={() => {
                                        scrollToSection(item.id);
                                        setIsMenuOpen(false);
                                    }}
                                >
                                    {item.label}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
                <div className="nav-links">
                    <button className="btn-nav-secondary" onClick={() => navigate("/demo")}>Try the Demo</button>
                    <button className="btn-nav-primary" onClick={handleStartTrial}>
                        {isAuthenticated ? "Go to Studio" : "Start Free Trial"}
                    </button>
                    {isAuthenticated && <UserMenu />}
                </div>
            </nav>

            <HeroSection onStartTrial={handleStartTrial} />

            <AuthModal
                isOpen={showAuthModal}
                onClose={() => setShowAuthModal(false)}
                onSuccess={() => navigate("/app")}
            />
            <WaitlistModal
                isOpen={showWaitlistModal}
                onClose={() => setShowWaitlistModal(false)}
                source={waitlistSource}
            />

            <section
                className="landing-section what-it-does-section"
                id="what-it-does"
                ref={whatItDoesRef}
            >
                <h2 className="section-title">What it does</h2>
                <p className="section-subtitle">Turn your MusicXML score into a singing demo in minutes.</p>
                <div className="what-it-does-layout">
                    <div className="what-it-does-stage" aria-hidden="true" />
                    <div className="what-it-does-cards">
                        <div className="use-cases-grid what-it-does-grid">
                            <div className="use-case-card">
                                <h3>Score → Singing Demo</h3>
                                <p className="description">Upload MusicXML and get a realistic vocal preview without a DAW.</p>
                            </div>
                            <div className="use-case-card">
                                <h3>Chat-driven Takes</h3>
                                <p className="description">Ask for parts, verses, and style changes using natural language.</p>
                            </div>
                            <div className="use-case-card">
                                <h3>Fast Iteration</h3>
                                <p className="description">Generate multiple interpretations quickly for practice or review.</p>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-section" id="who-for">
                <h2 className="section-title">Who is SightSinger.ai for?</h2>
                <div className="use-cases-grid">
                    <div className="use-case-card">
                        <h3>Indie Songwriters</h3>
                        <p>"Instant vocal demo"</p>
                        <p className="description">Get a convincing singing preview fast—no session singer, no studio time.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Choir &amp; Worship Leaders</h3>
                        <p>"Parts in minutes"</p>
                        <p className="description">Generate SATB (or melody) practice tracks in minutes—save pianist hours of practice and recording time.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Beginner Singers</h3>
                        <p>"Stepping stone"</p>
                        <p className="description">Try singing with a score-accurate guide before investing in expensive lessons.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Quick Song Learners</h3>
                        <p>"Learn it fast"</p>
                        <p className="description">Pick up a few songs quickly for occasions without diving into theory or breath training.</p>
                    </div>
                </div>
            </section>

            <section className="landing-section alt-bg" id="not-for">
                <h2 className="section-title">SightSinger.ai is <i>NOT</i>...</h2>
                <div className="use-cases-grid">
                    <div className="use-case-card">
                        <h3>A DAW replacement</h3>
                        <p className="description">It doesn’t replace professional vocal synthesis tools. But it can be used to complement them with fast, score-based vocal previews.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>A human vocalist</h3>
                        <p className="description">It’s a fast, score-accurate singing demo for rehearsal and practice. It doesn't replace human singers or human recordings.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>A Song Generator</h3>
                        <p className="description">It doesn’t generate songs from text prompts (like Suno). It sings the music you’ve already written and follows the score exactly.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>A Voice Converter</h3>
                        <p className="description">It doesn’t convert recorded vocals or require a base audio track. It sings directly from the score.</p>
                    </div>
                </div>
            </section>

            <section className="landing-section" id="why">
                <h2 className="section-title">Why SightSinger.ai?</h2>
                <p className="section-subtitle">Speak music, not MIDI.</p>

                <div className="comparison-container">
                    <table className="comparison-table">
                        <thead>
                            <tr>
                                <th>Feature</th>
                                <th className="highlight">SightSinger.ai</th>
                                <th>Professional DAW (Digital Audio Workstation)</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td>Interface</td>
                                <td className="highlight">Natural Language (Chat)</td>
                                <td>Piano roll, parameters, phonemes</td>
                            </tr>
                            <tr>
                                <td>Learning Curve</td>
                                <td className="highlight">Zero</td>
                                <td>Steep</td>
                            </tr>
                            <tr>
                                <td>Focus</td>
                                <td className="highlight">Global style control, demo quality</td>
                                <td>Detailed note-level control, production quality</td>
                            </tr>
                            <tr>
                                <td>Time to result</td>
                                <td className="highlight">Minutes</td>
                                <td>Hours</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </section>
            <section className="landing-section compact" id="how-it-works">
                <h2 className="section-title">How it works</h2>
                <div className="timeline">
                    <div className="timeline-row">
                        <div className="timeline-step">01</div>
                        <div className="timeline-content">
                            <h3>Upload your score</h3>
                            <p>Drop in MusicXML from MuseScore, Logic Pro, Finale, or Sibelius. <br />We parse tempo maps, part labels, and lyric syllables with music21.</p>
                        </div>
                    </div>
                    <div className="timeline-row">
                        <div className="timeline-step">02</div>
                        <div className="timeline-content">
                            <h3>Tell the singer</h3>
                            <p>Use natural language to pick parts/verses and shape phrasing, tone, and expression. <br />Gemini Flash 3 calls internal MCP tools to map intent into performance parameters.</p>
                        </div>
                    </div>
                    <div className="timeline-row">
                        <div className="timeline-step">03</div>
                        <div className="timeline-content">
                            <h3>Generate the singing voice</h3>
                            <p>SightSinger.ai runs a custom singing synthesis pipeline to render a realistic vocal demo directly from the score. <br />Voicebanks are DiffSinger-compatible OpenUtau ONNX models, so adding new voices is straightforward.</p>
                        </div>
                    </div>
                    <div className="timeline-row">
                        <div className="timeline-step">04</div>
                        <div className="timeline-content">
                            <h3>Refine and share</h3>
                            <p>Iterate quickly, render new takes, and export a shareable demo for singers.</p>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-section" id="ai-voices">
                <h2 className="section-title">AI Voices</h2>
                <p className="section-subtitle voicebanks-subtitle">
                    Current AI voices are <span className="highlight-text">not cleared for commercial use</span>. <br/>Royalty-free voices will be added when paid plans launch.
                </p>
                <div className="voicebanks-grid">
                    <div className="voicebank-card">
                        <img
                            src="/voicebanks/reizo_icon.png"
                            alt="Raine Reizo icon"
                            className="voicebank-icon"
                            loading="lazy"
                        />
                        <div className="voicebank-body">
                            <h3>Raine Reizo</h3>
                            <p className="voicebank-profile">
                                Version 2.0 · by suyu (UtauReizo) · 3 tone colors (normal/soft/strong)
                            </p>
                        </div>
                    </div>
                    <div className="voicebank-card">
                        <img
                            src="/voicebanks/rena_icon.png"
                            alt="Raine Rena icon"
                            className="voicebank-icon"
                            loading="lazy"
                        />
                        <div className="voicebank-body">
                            <h3>Raine Rena</h3>
                            <p className="voicebank-profile">
                                Version 2.0 · by suyu (UtauReizo) · 3 tone colors (normal/soft/strong)
                            </p>
                        </div>
                    </div>
                </div>
                <div className="voicebank-credits">
                    <div className="voicebank-credits-title">CREDITS</div>
                    <div>Website: <a href="https://rainerr.weebly.com/" target="_blank" rel="noreferrer">https://rainerr.weebly.com/</a></div>
                    <div>Voiced, labelled &amp; trained by suyu (UtauReizo)</div>
                    <div className="voicebank-credits-subtitle">Datasets used for parallel training:</div>
                    <ul>
                        <li>Raine Rena</li>
                        <li>Fromage</li>
                        <li>PJS (relabel by UtaUtaUtau)</li>
                        <li>CSD (relabel by heta-tan)</li>
                        <li>opencpop (relabel by komisteng)</li>
                        <li>m4singer (relabel by nobodyP)</li>
                        <li>TGM</li>
                    </ul>
                </div>
            </section>

            <section className="landing-section" id="pricing">
                <h2 className="section-title">
                    Pricing <span className="section-title-note">(Will be announced soon)</span>
                </h2>
                <p className="section-subtitle">
                    Credits keep usage predictable. Each credit covers 30 seconds of generated audio.
                </p>
                <div className="use-cases-grid">
                    <div className="use-case-card">
                        <h3>Credit Based Subscription</h3>
                        <p className="description">Paid plan credits will carry a 1‑year expiry so you can cover peak seasons like Christmas.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Pay Only For Output</h3>
                        <p className="description">Credits are reserved pre-render, and consumed when audio is delivered, rounded up to the nearest 30 seconds.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Flexible Usage</h3>
                        <p className="description">Spend credits on one long take or many short parts and verses—it’s up to you.</p>
                    </div>
                </div>
            </section>

            <section className="landing-section alt-bg" id="faq">
                <h2 className="section-title">FAQ</h2>
                <div className="faq-list">
                    {[
                        {
                            key: "alternatives",
                            q: "Why not use ACE Studio, Cantai, or OpenUtau?",
                            a: "Those tools are built for detailed vocal production and require note-by-note or phoneme editing. SightSinger.ai generates quick song previews using natural language directions, without DAW or MIDI knowledge.",
                        },
                        {
                            key: "formats",
                            q: "What file format do you support?",
                            a: "MusicXML (.xml or compressed .mxl) from any MuseScore, Logic Pro, Finale, or Sibelius.",
                        },
                        {
                            key: "pdf",
                            q: "Can I upload a PDF score instead of MusicXML?",
                            a: (
                                <>
                                    Not yet. I'd love to support PDF-to-singing in the future, but turning PDFs into
                                    accurate MusicXML is already a complex problem and takes time to get right. Also,
                                    there are already good online tools by MuseScore and ACE Studio that convert PDF
                                    scores to MusicXML.
                                    <br />
                                    You can use those to convert PDF to MusicXML first, then upload the MusicXML file
                                    here to hear your song.
                                </>
                            ),
                        },
                        {
                            key: "royalty",
                            q: "Are the AI voices in SightSinger.ai royalty-free for music production?",
                            a: "Not yet. The current voicebanks are for demo use only and not cleared for commercial release. Royalty-free voices will be introduced once paid plans launch.",
                        },
                    ].map((item, index) => {
                        const isOpen = openFaqIndex === index;
                        return (
                            <div className={`faq-item ${isOpen ? "open" : ""}`} key={item.key}>
                                <button
                                    className="faq-question"
                                    type="button"
                                    onClick={() => setOpenFaqIndex(isOpen ? null : index)}
                                >
                                    <span>{item.q}</span>
                                    <span className="faq-toggle" aria-hidden="true">
                                        {isOpen ? "–" : "+"}
                                    </span>
                                </button>
                                {isOpen && <div className="faq-answer">{item.a}</div>}
                            </div>
                        );
                    })}
                </div>
            </section>

            <section id="about" className="landing-section">
                <h2 className="section-title">About Me</h2>
                <div className="about-content">
                    <div className="bio-list">
                        <p className="bio-paragraph">
                            I'm Alan, a software engineer and volunteer musician. I love building things that solve real problems. I'm passionate about using AI to solve my own problem in music, and hopefully it can help you too.
                        </p>
                        <div className="bio-item">
                            <span className="bio-label">Background</span>
                            <span className="bio-value">
                                <span>Software engineering: 20+ years (full-stack)</span>
                                <span>Music Instruments: Piano, drums, ukulele</span>
                                <span>Active in a church choir and community concert band</span>
                                <span>Music tech: Logic Pro</span>
                                <span>AI singing / vocal synthesis: OpenUtau, DiffSinger</span>
                            </span>
                        </div>
                        <div className="bio-socials" aria-label="Social links">
                            <a
                                className="bio-social"
                                href="https://www.instagram.com/twittlealan/"
                                target="_blank"
                                rel="noreferrer"
                                aria-label="Instagram"
                            >
                                <Instagram size={18} />
                            </a>
                            <a
                                className="bio-social"
                                href="https://discord.com/users/littlealan1915"
                                target="_blank"
                                rel="noreferrer"
                                aria-label="Discord"
                            >
                                <MessageCircle size={18} />
                            </a>
                            <a
                                className="bio-social"
                                href="https://www.reddit.com/user/littleAlanYT/"
                                target="_blank"
                                rel="noreferrer"
                                aria-label="Reddit"
                            >
                                <MessageSquare size={18} />
                            </a>
                            <a
                                className="bio-social"
                                href="mailto:littlealan@gmail.com"
                                aria-label="Email"
                            >
                                <Mail size={18} />
                            </a>
                        </div>
                    </div>
                </div>
            </section>

            <footer className="landing-footer">
                <div className="footer-content">
                    <div className="footer-brand">
                        <Sparkles size={24} />
                        <span>SightSinger.ai</span>
                    </div>
                    <div className="footer-links">
                        <a href="https://github.com/littlealan-dev/ai-singer-diffsinger" target="_blank" rel="noreferrer">GitHub</a>
                        <a href="#about">About Me</a>
                        <button className="footer-waitlist" onClick={() => handleJoinWaitlist("landing")}>
                            Join Waiting List
                        </button>
                    </div>
                </div>
                <p className="copyright">© 2026 SightSinger.ai.</p>
            </footer>
        </div>
    );
}
