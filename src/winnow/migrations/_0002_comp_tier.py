"""Record which compensation floor applies to each employer.

The floors are tiered — $200k mid-market, $275k large-corporate, $400k in crypto
— and which one applies is a fact about the company, not about the posting. The
seed list already marks it; without somewhere to put it the gate would have to
assume, and assuming the higher floor silently discards roles that clear the one
that actually applies to them.
"""

SQL = """
ALTER TABLE companies ADD COLUMN comp_tier TEXT NOT NULL DEFAULT 'mid_market'
    CHECK (comp_tier IN ('mid_market', 'large_corporate', 'crypto'));
"""
