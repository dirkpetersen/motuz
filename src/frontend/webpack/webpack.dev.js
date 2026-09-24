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
        // Only accept other Host headers when explicitly listening on another interface
        allowedHosts: process.env.MOTUZ_HOST ? 'all' : 'auto',
        proxy: [{
            context: ['/api', '/swaggerui'],
            target: 'http://localhost:5000/',
        }],
    },

    devtool: isQuick ? false : 'eval-cheap-source-map',
});
