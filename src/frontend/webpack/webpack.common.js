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

    // Size hints only for what every visitor downloads at start. The viewer chunks
    // (viewer-*, sheetjs, the pdf.js worker), the pdf.js assets copied by
    // PdfjsAssetsPlugin (wasm, cmaps, standard fonts) are loaded on demand when a file
    // is opened (see .claude/rules/file-viewers.md), so their size is by design.
    // The app bundle includes bootstrap CSS and react-dom and is cached for 30 days
    // under a content hash; the limits are above its size so only a real regression
    // warns. No runtimeChunk: the UI tests and the page expect a single start script.
    performance: {
        maxAssetSize: 1.5 * 1024 * 1024,
        maxEntrypointSize: 1.5 * 1024 * 1024,
        assetFilter: (name) => /^js\/app-[0-9a-f]+\.bundle\.js$/.test(name),
    },

    stats: {
        colors: true
    },
};
