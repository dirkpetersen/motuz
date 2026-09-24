const path = require('path');

const HtmlWebpackPlugin = require('html-webpack-plugin');

module.exports = {
    entry: {
        app: './src/frontend/js/main.jsx',
    },

    output: {
        path: path.resolve(__dirname, '..', '..', '..', 'build'),
        filename: 'js/[name]-[contenthash].bundle.js',
        clean: true,
        publicPath: '/',
    },

    module: {
        rules: [{
            test: /\.css$/,
            use: [
                'style-loader',
                'css-loader'
            ]
        }, {
            test: /\.(js|jsx)$/,
            exclude: /node_modules/,
            use: {
                loader: "babel-loader"
            }
        }, {
            test: /\.(woff|woff2|eot|ttf)$/,
            type: 'asset',
            parser: { dataUrlCondition: { maxSize: 100000 } },
        }, {
            test: /\.(png|svg|jpg|gif|ico)$/,
            type: 'asset/resource',
            generator: { filename: 'img/[name][ext]' },
        }]
    },

    plugins: [
        new HtmlWebpackPlugin({
            filename: './index.html',
            template: './src/frontend/index.html',
            title: 'WebApp',
            minify: true,
            meta: {
            }
        }),
    ],

    resolve: {
        modules: [
            path.resolve('./src/frontend/js'),
            path.resolve('./src/frontend/css'),
            path.resolve('./src/frontend/img'),
            path.resolve('./node_modules')
        ],
        extensions: ['.js', '.jsx', '...'],
        fallback: {
            path: require.resolve('path-browserify'), // For upath
        },
    },

    stats: {
        colors: true
    },
};
