import { useState, useEffect } from 'react';
import { doc, onSnapshot } from 'firebase/firestore';
import { db, getIdToken } from '../firebase';
import { useAuth } from './useAuth.tsx';

export interface UserCredits {
    balance: number;
    reserved: number;
    available: number;
    expiresAt: Date | null;
    overdrafted: boolean;
    isExpired: boolean;
    loading: boolean;
}

export function useCredits(): UserCredits {
    const { user } = useAuth();
    const [credits, setCredits] = useState<Omit<UserCredits, 'loading'>>({
        balance: 0,
        reserved: 0,
        available: 0,
        expiresAt: null,
        overdrafted: false,
        isExpired: false,
    });
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        if (!user) {
            setLoading(false);
            return;
        }

        const ensureCredits = async () => {
            try {
                const token = await getIdToken();
                if (!token) return;
                await fetch(`${import.meta.env.VITE_API_BASE}/credits`, {
                    headers: { Authorization: `Bearer ${token}` },
                });
            } catch (error) {
                console.error("Error ensuring credits:", error);
            }
        };

        ensureCredits();

        const userDocRef = doc(db, 'users', user.uid);

        const unsubscribe = onSnapshot(userDocRef, (snapshot) => {
            if (snapshot.exists()) {
                const data = snapshot.data();
                const creditsData = data?.credits || {};

                const balance = creditsData.balance || 0;
                const reserved = creditsData.reserved || 0;
                const available = balance - reserved;
                const expiresAt = creditsData.expiresAt?.toDate() || null;
                const overdrafted = creditsData.overdrafted || false;
                const isExpired = expiresAt ? new Date() > expiresAt : false;

                setCredits({
                    balance,
                    reserved,
                    available,
                    expiresAt,
                    overdrafted,
                    isExpired,
                });
            }
            setLoading(false);
        }, (error) => {
            console.error("Error listening to credits:", error);
            setLoading(false);
        });

        return () => unsubscribe();
    }, [user]);

    return { ...credits, loading };
}
