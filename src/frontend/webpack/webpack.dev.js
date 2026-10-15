const { merge } = require('webpack-merge');

const common = require('./webpack.common.js');

// Change this to toggle debugging bloat
const isQuick = false;

const host = process.env.MOTUZ_HOST || 'localhost';

module.exports = merge(common, {
    mode: 'development',

    devServer: {
        port: 8080,
        host: host,
        static: false, // Everything is served from memory by webpack
        historyApiFallback: true, // For ReactRouter
        // Resources only from this server, like views/frontend_views.py APP_CSP in production
        headers: {
            'Content-Security-Policy': "img-src 'self' blob: data:; media-src 'self' blob:; font-src 'self' blob: data:; "
                + "connect-src 'self' blob: data: ws: wss:; worker-src 'self' blob:; frame-src 'none'; object-src 'none'; "
                + "base-uri 'self'",
        },
        // Only accept other Host headers when explicitly listening on another interface
        allowedHosts: process.env.MOTUZ_HOST ? 'all' : 'auto',
        proxy: [{
            // /privacy and /terms are server-side pages (views/legal_views.py)
            context: ['/api', '/swaggerui', '/about', '/privacy', '/terms'],
            target: 'http://localhost:5000/',
        }],
    },

    devtool: isQuick ? false : 'eval-cheap-source-map',
});
