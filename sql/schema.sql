-- Portable analytics schema. Adjust VARCHAR/DECIMAL syntax for your warehouse.
CREATE TABLE dim_date (
    date_key INTEGER PRIMARY KEY,
    date_value DATE NOT NULL,
    year INTEGER NOT NULL,
    quarter VARCHAR(2) NOT NULL,
    month_number INTEGER NOT NULL,
    month_name VARCHAR(12) NOT NULL,
    year_month VARCHAR(7) NOT NULL,
    week_number INTEGER NOT NULL,
    day_name VARCHAR(12) NOT NULL,
    is_weekend INTEGER NOT NULL
);

CREATE TABLE dim_channel (
    channel_key INTEGER PRIMARY KEY,
    distribution_channel VARCHAR(100) NOT NULL
);

CREATE TABLE dim_segment (
    segment_key INTEGER PRIMARY KEY,
    market_segment VARCHAR(100) NOT NULL
);

CREATE TABLE dim_hotel (
    hotel_key INTEGER PRIMARY KEY,
    hotel VARCHAR(100) NOT NULL
);

CREATE TABLE fact_bookings (
    booking_id VARCHAR(12) PRIMARY KEY,
    date_key INTEGER,
    channel_key INTEGER,
    segment_key INTEGER,
    hotel_key INTEGER,
    country VARCHAR(3),
    booking_status VARCHAR(20),
    is_canceled INTEGER,
    lead_time INTEGER,
    total_nights INTEGER,
    total_guests DECIMAL(8,2),
    adr_clean DECIMAL(14,2),
    gross_booking_value DECIMAL(16,2),
    realized_revenue DECIMAL(16,2),
    valid_for_ml INTEGER,
    FOREIGN KEY (date_key) REFERENCES dim_date(date_key),
    FOREIGN KEY (channel_key) REFERENCES dim_channel(channel_key),
    FOREIGN KEY (segment_key) REFERENCES dim_segment(segment_key),
    FOREIGN KEY (hotel_key) REFERENCES dim_hotel(hotel_key)
);

CREATE INDEX idx_fact_bookings_date ON fact_bookings(date_key);
CREATE INDEX idx_fact_bookings_channel ON fact_bookings(channel_key);
CREATE INDEX idx_fact_bookings_segment ON fact_bookings(segment_key);

-- fact_market_arrivals is a contextual market table. State of entry must not be
-- interpreted as the traveller's final destination.
CREATE TABLE fact_market_arrivals (
    date_key INTEGER,
    country VARCHAR(3),
    state_of_entry VARCHAR(100),
    arrivals INTEGER,
    arrivals_male INTEGER,
    arrivals_female INTEGER,
    gender_reconciliation_gap INTEGER
);

-- Campaign data in this portfolio is synthetic and must be replaced by an
-- actual CRM/Ads/API export before any business use.
CREATE TABLE fact_campaign_funnel (
    campaign_id VARCHAR(12) PRIMARY KEY,
    date_key INTEGER,
    campaign_name VARCHAR(200),
    channel VARCHAR(100),
    target_segment VARCHAR(100),
    sent INTEGER,
    delivered INTEGER,
    opened INTEGER,
    clicks INTEGER,
    leads INTEGER,
    bookings INTEGER,
    spend_myr DECIMAL(16,2),
    attributed_revenue_myr DECIMAL(16,2),
    data_type VARCHAR(100)
);
