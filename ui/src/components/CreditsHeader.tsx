import React from 'react';
import type { UserCredits } from '../hooks/useCredits';
import { Calendar, Flame, AlertCircle } from 'lucide-react';
import './CreditsHeader.css';

type CreditsHeaderProps = Pick<
    UserCredits,
    'available' | 'isExpired' | 'overdrafted' | 'loading'
> & {
    nextCreditRefreshAt: Date | null;
};

const CreditsHeader: React.FC<CreditsHeaderProps> = ({
    available,
    nextCreditRefreshAt,
    isExpired,
    overdrafted,
    loading,
}) => {

    if (loading) return <div className="credits-pill loading">...</div>;

    const daysUntilReset = nextCreditRefreshAt
        ? Math.max(0, Math.ceil((nextCreditRefreshAt.getTime() - Date.now()) / (1000 * 60 * 60 * 24)))
        : null;

    let statusClass = 'normal';
    let icon = <Flame size={14} className="icon" />;
    let label = `${available} Credits`;

    if (overdrafted || isExpired || available <= 0) {
        statusClass = 'danger';
        icon = <AlertCircle size={14} className="icon" />;
        if (overdrafted) label = `${available} Credits (Overdraft)`;
        else if (isExpired) label = `${available} Credits (Expired)`;
        else label = '0 Credits';
    } else if (available <= 2) {
        statusClass = 'warning';
    }

    return (
        <div
            className={`credits-pill ${statusClass}`}
            title={isExpired ? "Credits have expired" : overdrafted ? "Account locked due to negative balance" : "Credit balance"}
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
        </div>
    );
};

export default CreditsHeader;
