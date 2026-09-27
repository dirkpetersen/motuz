const fs = require('fs');
const path = require('path');

const HtmlWebpackPlugin = require('html-webpack-plugin');
const {DefinePlugin: compilerDefine} = require('webpack');

// pdf.js loads character maps (CJK fonts), the standard fonts (PDFs that do not embed
// Helvetica, Times, ...) and its WebAssembly image/color decoders at run time. They
// are served from this build (under /js, never from a CDN), in a folder named after
// the version because their names carry no content hash. The scripting sandbox
// (quickjs) is left out: PDF JavaScript is never run.
const PDFJS_DIR = path.dirname(require.resolve('pdfjs-dist/package.json'));
const PDFJS_VERSION = require('pdfjs-dist/package.json').version;
const PDFJS_ASSET_DIR = `js/pdfjs-${PDFJS_VERSION}`;
const PDFJS_ASSETS = ['cmaps', 'standard_fonts', 'wasm'];

class PdfjsAssetsPlugin {
    apply(compiler) {
        const {sources, Compilation} = compiler.webpack;
        compiler.hooks.thisCompilation.tap('PdfjsAssetsPlugin', (compilation) => {
            compilation.hooks.processAssets.tap(
                {name: 'PdfjsAssetsPlugin', stage: Compilation.PROCESS_ASSETS_STAGE_ADDITIONAL},
                () => {
                    for (const folder of PDFJS_ASSETS) {
                        const dir = path.join(PDFJS_DIR, folder);
                        for (const name of fs.readdirSync(dir)) {
                            if (name.startsWith('quickjs')) {
                                continue;
                            }
                            const file = path.join(dir, name);
                            compilation.fileDependencies.add(file);
                            compilation.emitAsset(`${PDFJS_ASSET_DIR}/${folder}/${name}`,
                                new sources.RawSource(fs.readFileSync(file)));
                        }
                    }
                },
            );
        });
    }
}

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
        new PdfjsAssetsPlugin(),
        new compilerDefine({
            PDFJS_ASSET_BASE: JSON.stringify(`/${PDFJS_ASSET_DIR}/`),
        }),
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
