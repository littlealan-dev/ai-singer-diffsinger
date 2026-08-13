import React, { useEffect, useRef, useState } from 'react';
import type { UserCredits } from '../hooks/useCredits';
import { Calendar, Flame, AlertCircle } from 'lucide-react';
import type { BillingTopupPack } from '../hooks/useBillingState';
import type { PlanFamily } from '../billing/plans';
import './CreditsHeader.css';

type CreditsHeaderProps = Pick<
    UserCredits,
    'available' | 'subscriptionAvailable' | 'topupAvailable' | 'topupEarliestExpiresAt' | 'isExpired' | 'overdrafted' | 'loading' | 'error'
> & {
    nextCreditRefreshAt: Date | null;
    planFamily: PlanFamily;
    topupPacks: BillingTopupPack[];
};

const CreditsHeader: React.FC<CreditsHeaderProps> = ({
    available,
    subscriptionAvailable,
    topupAvailable,
    topupEarliestExpiresAt,
    nextCreditRefreshAt,
    isExpired,
    overdrafted,
    loading,
    error,
    planFamily,
    topupPacks,
}) => {
    const [breakdownOpen, setBreakdownOpen] = useState(false);
    const containerRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (!breakdownOpen) return;
        const handlePointerDown = (event: PointerEvent) => {
            if (!containerRef.current?.contains(event.target as Node)) {
                setBreakdownOpen(false);
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                setBreakdownOpen(false);
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [breakdownOpen]);

    if (error) return <div className="credits-pill danger" title={error}>Credits unavailable</div>;
    if (loading) return <div className="credits-pill loading">...</div>;

    const daysUntilReset = nextCreditRefreshAt
        ? Math.max(0, Math.ceil((nextCreditRefreshAt.getTime() - Date.now()) / (1000 * 60 * 60 * 24)))
        : null;

    let statusClass = 'normal';
    let icon = <Flame size={14} className="icon" />;
    let label = topupAvailable > 0
        ? `${Math.max(0, subscriptionAvailable)} + ${topupAvailable} bonus`
        : `${available} Credits`;
    const topupExpiryLabel = topupEarliestExpiresAt
        ? `; bonus expires ${topupEarliestExpiresAt.toLocaleDateString()}`
        : "";

    if (overdrafted || isExpired || available <= 0) {
        statusClass = 'danger';
        icon = <AlertCircle size={14} className="icon" />;
        if (overdrafted) label = `${available} Credits (Overdraft)`;
        else if (isExpired) label = `${available} Credits (Expired)`;
        else label = '0 Credits';
    } else if (available <= 2) {
        statusClass = 'warning';
    }

    const monthlyLabel = `Monthly (${formatPlanFamily(planFamily)})`;
    const breakdownRows = [
        {
            source: monthlyLabel,
            credits: Math.max(0, subscriptionAvailable),
            expiry: nextCreditRefreshAt ? `Resets ${formatShortDate(nextCreditRefreshAt)}` : "--",
        },
        ...topupPacks.map((pack, index) => ({
            source: `Credit Pack #${index + 1}`,
            credits: pack.creditsAvailable,
            expiry: pack.expiresAt ? `Expires ${formatShortDate(pack.expiresAt)}` : "--",
        })),
    ];

    return (
        <div className="credits-popover-anchor" ref={containerRef}>
            <button
                type="button"
                className={`credits-pill ${statusClass}`}
                title={isExpired ? "Credits have expired" : overdrafted ? "Account locked due to negative balance" : `${Math.max(0, subscriptionAvailable)} monthly credits + ${topupAvailable} bonus credits${topupExpiryLabel}`}
                aria-haspopup="dialog"
                aria-expanded={breakdownOpen}
                onClick={() => setBreakdownOpen((open) => !open)}
            >
                <div className="credits-main">
                    {icon}
                    <span className="credits-label">{label}</span>
                </div>
                {!overdrafted && !isExpired && (
                    <div className="credits-divider" />
                )}
                {!overdrafted && !isExpired && (
                    <div className="credits-expiry">
                        <Calendar size={12} className="icon-small" />
                        <span>{daysUntilReset === null ? "--" : `${daysUntilReset}d to reset`}</span>
                    </div>
                )}
            </button>
            {breakdownOpen && (
                <div className="credits-breakdown-popover" role="dialog" aria-label="Credit breakdown">
                    <table className="credits-breakdown-table">
                        <thead>
                            <tr>
                                <th>Source</th>
                                <th>Credits</th>
                                <th>Expiry</th>
                            </tr>
                        </thead>
                        <tbody>
                            {breakdownRows.map((row) => (
                                <tr key={row.source}>
                                    <td>{row.source}</td>
                                    <td>{row.credits}</td>
                                    <td>{row.expiry}</td>
                                </tr>
                            ))}
                        </tbody>
                        <tfoot>
                            <tr>
                                <td>Total</td>
                                <td>{available}</td>
                                <td />
                            </tr>
                        </tfoot>
                    </table>
                </div>
            )}
        </div>
    );
};

function formatPlanFamily(planFamily: PlanFamily): string {
    if (planFamily === 'solo') return 'Solo';
    if (planFamily === 'choir') return 'Choir';
    return 'Free';
}

function formatShortDate(date: Date): string {
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: '2-digit' });
}

export default CreditsHeader;
